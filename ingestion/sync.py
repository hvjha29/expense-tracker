import base64
from datetime import datetime
from db.manager import DatabaseManager
from ingestion.gmail_client import GmailClient
from ingestion.parsers.hdfc_parser import HDFCParser
from ingestion.parsers.axis_parser import AxisParser

class SyncService:
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self.gmail = GmailClient()
        self.parsers = [
            HDFCParser(),
            AxisParser()
        ]

    def sync_emails(self, days=7):
        """
        Fetches transaction emails from Gmail for the last N days.
        Uses multiple targeted queries with absolute dates for reliability.
        """
        import datetime
        after_date = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime('%Y/%m/%d')
        
        queries = [
            f'\"HDFC Bank\" \"payment was made\" after:{after_date}',
            f'\"HDFC Bank\" \"debited\" after:{after_date}',
            f'\"HDFC Bank\" \"UPI txn\" after:{after_date}',
            f'\"Axis Bank\" \"Transaction alert\" after:{after_date}',
            f'label:\"Bank updates\" after:{after_date}'
        ]
        
        all_messages = []
        seen_ids = set()
        
        for query in queries:
            messages = self.gmail.list_messages(query=query, max_results=200)
            for m in messages:
                if m['id'] not in seen_ids:
                    all_messages.append(m)
                    seen_ids.add(m['id'])
        
        synced_count = 0
        error_count = 0
        
        for msg_ref in all_messages:
            msg_id = msg_ref['id']
            
            # 1. Check if already processed
            if self.db.fetch_one("SELECT 1 FROM ingestion_log WHERE email_id = ?", (msg_id,)):
                continue

            # 2. Fetch full message
            msg = self.gmail.get_message(msg_id)
            if not msg:
                continue

            headers = msg.get('payload', {}).get('headers', [])
            subject = next((h['value'] for h in headers if h['name'] == 'Subject'), "")
            
            # Extract and parse the Date header
            from email.utils import parsedate_to_datetime
            date_str = next((h['value'] for h in headers if h['name'] == 'Date'), None)
            email_date = None
            if date_str:
                try:
                    # Convert to naive local time for consistency with SQLite
                    email_date = parsedate_to_datetime(date_str).replace(tzinfo=None)
                except:
                    pass
            
            # Extract body
            body = ""
            def get_body(payload):
                if 'parts' in payload:
                    for part in payload['parts']:
                        res = get_body(part)
                        if res: return res
                if payload.get('mimeType') in ['text/plain', 'text/html']:
                    return base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8')
                return None
            
            body = get_body(msg['payload'])
            if not body:
                continue

            # 3. Parse with all available parsers
            try:
                parsed_data = None
                for parser in self.parsers:
                    parsed_data = parser.parse(subject, body, email_date=email_date)
                    if parsed_data:
                        break
                
                if parsed_data:
                    # 4. Save Transaction
                    self.db.execute(
                        """
                        INSERT INTO transactions 
                        (txn_date, amount, merchant_raw, instrument_last4, payment_method, source_email_id, txn_ref)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            parsed_data['timestamp'],
                            parsed_data['amount'],
                            parsed_data['merchant'],
                            parsed_data['instrument_last4'],
                            parsed_data['type'],
                            msg_id,
                            parsed_data.get('txn_ref')
                        )
                    )
                    
                    # Log success
                    self.db.execute(
                        "INSERT INTO ingestion_log (email_id, status) VALUES (?, ?)",
                        (msg_id, 'parsed')
                    )
                    synced_count += 1
                else:
                    # Log ignored (not a transaction email we know)
                    self.db.execute(
                        "INSERT INTO ingestion_log (email_id, status) VALUES (?, ?)",
                        (msg_id, 'ignored')
                    )
            except Exception as e:
                # Log failure
                self.db.execute(
                    "INSERT INTO ingestion_log (email_id, status, error_message) VALUES (?, ?, ?)",
                    (msg_id, 'failed', str(e))
                )
                error_count += 1

        return synced_count, error_count

    def sync_hdfc_emails(self, days=7):
        # Legacy method wrapper
        return self.sync_emails(days=days)

if __name__ == "__main__":
    db_manager = DatabaseManager()
    sync_service = SyncService(db_manager)
    synced, errors = sync_service.sync_emails(days=365)
    print(f"Sync Complete: {synced} transactions added, {errors} errors.")
