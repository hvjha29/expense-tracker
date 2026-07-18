import re
import html
from datetime import datetime

from ingestion.amount_utils import infer_direction, normalize_amount


class HDFCParser:
    """
    Parser for HDFC Bank transaction emails.
    Handles legacy (.net) and new (.bank.in) templates for CC and UPI.
    """

    def __init__(self):
        # Regex for Amount: Rs.481.00 or Rs. 602.00
        self.amt_re = re.compile(r'Rs\.?\s*([\d,]+\.\d{2})')
        
        # Regex for CC ending: ending 1941 or (ending 1941)
        self.cc_re = re.compile(r'Credit Card (?:ending|[\(]ending) (\d{4})')
        
        # Regex for Account ending: account 6167 or account ending 6167
        self.acct_re = re.compile(r'account (?:ending )?(\d{4})')

    def parse(self, subject, body_html, email_date=None):
        """
        Main entry point for parsing an email.
        body_html: The raw HTML content of the email.
        email_date: Optional datetime object representing when the email was received.
        """
        # 1. Decode HTML entities (&#39; -> ')
        body = html.unescape(body_html)
        
        # 2. Strip ALL HTML tags to avoid bolding/formatting breaking regex
        body = re.sub(r'<.*?>', ' ', body)
        
        # 3. Normalize whitespace
        body = " ".join(body.split())
        
        # 4. Determine Transaction Class
        result = None
        if "UPI txn" in subject:
            result = self._parse_upi(body)
            # UPI alerts lack time; use email_date's time if available
            if result and email_date and result['timestamp'].hour == 0 and result['timestamp'].minute == 0:
                result['timestamp'] = result['timestamp'].replace(
                    hour=email_date.hour, 
                    minute=email_date.minute, 
                    second=email_date.second
                )
        elif "Credit Card" in subject or "payment was made" in subject.lower():
            result = self._parse_cc(body)
            
        return result

    def _parse_cc(self, body):
        """
        Handles Template A (Legacy) and Template B (New) for CC POS/Online.
        """
        amt_match = self.amt_re.search(body)
        cc_match = self.cc_re.search(body)
        
        # Date pattern for CC: DD Mon, YYYY at HH:MM:SS
        # Example: 11 Jul, 2026 at 14:30:00
        date_match = re.search(r'(\d{1,2} [A-Za-z]{3}, \d{4} at \d{2}:\d{2}:\d{2})', body)
        
        # Merchant pattern: towards {merchant} on
        merchant_match = re.search(r'towards (.*?) on', body)
        
        if not (amt_match and cc_match and date_match):
            return None

        amount = normalize_amount(float(amt_match.group(1).replace(",", "")))
        direction = infer_direction(body=body, payment_type="CC_DEBIT")
        return {
            "type": "CC_DEBIT" if direction == "debit" else "CC_CREDIT",
            "amount": amount,
            "direction": direction,
            "instrument_last4": cc_match.group(1),
            "merchant": merchant_match.group(1).strip() if merchant_match else "Unknown",
            "timestamp": datetime.strptime(date_match.group(1), "%d %b, %Y at %H:%M:%S"),
            "raw_merchant": merchant_match.group(1).strip() if merchant_match else "",
        }

    def _parse_upi(self, body):
        """
        Handles Template C, D (Account UPI) and Template E (RuPay CC UPI).
        """
        amt_match = self.amt_re.search(body)
        
        # RRN pattern: reference number is {rrn} or reference no.: {rrn} or Reference Number: {rrn}
        rrn_match = re.search(r'(?:reference number is|reference no\.:|Reference Number:)\s*(\d+)', body, re.IGNORECASE)
        
        # Date pattern for UPI: DD-MM-YY
        date_match = re.search(r'(\d{2}-\d{2}-\d{2})', body)
        
        # Determine Instrument
        if "RuPay Credit Card" in body:
            instr_type = "RUPAY_CC_UPI"
            instr_match = self.cc_re.search(body)
        else:
            instr_type = "ACCOUNT_UPI"
            instr_match = self.acct_re.search(body)

        # Payee/VPA parsing
        # C/D: to VPA {payee_vpa} {payee_name} or towards VPA {payee_vpa} ({payee_name})
        # E: Paid to {payee_vpa}
        payee = "Unknown"
        if instr_type == "RUPAY_CC_UPI":
            p_match = re.search(r'Paid to ([^\s]+)', body)
            if p_match: payee = p_match.group(1)
        else:
            # Look for VPA pattern
            p_match = re.search(r'(?:to|towards) VPA ([\w\.\-]+@[\w]+)', body)
            if p_match: payee = p_match.group(1)

        if not (amt_match and rrn_match and date_match and instr_match):
            return None

        amount = normalize_amount(float(amt_match.group(1).replace(",", "")))
        direction = infer_direction(body=body, payment_type=instr_type)
        return {
            "type": instr_type,
            "amount": amount,
            "direction": direction,
            "instrument_last4": instr_match.group(1),
            "txn_ref": rrn_match.group(1),
            "merchant": payee,
            "timestamp": datetime.strptime(date_match.group(1), "%d-%m-%y"),
            "raw_merchant": payee,
        }

if __name__ == "__main__":
    # Test cases based on user examples
    parser = HDFCParser()
    
    # Test Template A
    t1_sub = "Rs.481.00 debited via Credit Card 1941"
    t1_body = "Rs.481.00 is debited from your HDFC Bank Credit Card ending 1941 towards Amazon on 11 Jul, 2026 at 14:30:00."
    print("Template A:", parser.parse(t1_sub, t1_body))

    # Test Template E
    t5_sub = "❗ You have done a UPI txn. Check details!"
    t5_body = "Rs. 602.00 has been debited from your RuPay Credit Card (ending 0666). Paid to merchant@upi. Date: 11-07-26. UPI Transaction Reference Number: 123456789012"
    print("Template E:", parser.parse(t5_sub, t5_body))
