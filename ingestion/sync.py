import base64
from datetime import datetime
from db.manager import DatabaseManager
from ingestion.gmail_client import GmailClient
from ingestion.parsers.hdfc_parser import HDFCParser

class SyncService:
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self.gmail = GmailClient()
        self.parser = HDFCParser()

    def sync_hdfc_emails(self, days=7):
        """
        Fetches HDFC emails from the last N days and parses them.
        """
        # HDFC uses two domains: @hdfcbank.net and @hdfcbank.bank.in
        query = f'from:(hdfcbank.net OR hdfcbank.bank.in) after:{days}d'
        messages = self.gmail.list_messages(query=query, max_results=100)
        
        synced_count = 0
        error_count = 0
        
        for msg_ref in messages:
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
            
            # Extract body
            body = ""
            if 'parts' in msg['payload']:
                for part in msg['payload']['parts']:
                    if part['mimeType'] == 'text/plain':
                        body = base64.urlsafe_b64decode(part['body']['data']).decode('utf-8')
                        break
                    elif part['mimeType'] == 'text/html':
                        body = base64.urlsafe_b64decode(part['body']['data']).decode('utf-8')
            else:
                body = base64.urlsafe_b64decode(msg['payload']['body']['data']).decode('utf-8')

            # 3. Parse
            try:
                parsed_data = self.parser.parse(subject, body)
                
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

if __name__ == "__main__":
    db_manager = DatabaseManager()
    sync_service = SyncService(db_manager)
    synced, errors = sync_service.sync_hdfc_emails(days=30)
    print(f"Sync Complete: {synced} transactions added, {errors} errors.")
