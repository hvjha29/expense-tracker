import re
import html
from datetime import datetime

from ingestion.amount_utils import infer_direction, normalize_amount


class AxisParser:
    """
    Parser for Axis Bank transaction emails.
    Template: Thank you for using your Card no. XX8248 for INR 2420.3 at BAG2BAG TRA on 28-06-24 11:45:42.
    """

    def __init__(self):
        # Regex for Amount: INR 2420.3
        self.amt_re = re.compile(r'INR\s*([\d,]+\.?\d*)')
        
        # Regex for Card ending: Card no. XX8248
        self.card_re = re.compile(r'Card no\. XX(\d{4})')

    def parse(self, subject, body_html, email_date=None):
        """
        Main entry point for parsing an email.
        """
        # Decode HTML entities
        body = html.unescape(body_html)
        # Normalize whitespace
        body = " ".join(body.split())
        
        # Check if it's an Axis transaction alert
        if "Transaction alert" not in subject and "Thank you for using your Card" not in body:
            return None

        amt_match = self.amt_re.search(body)
        card_match = self.card_re.search(body)
        
        # Date pattern for Axis: DD-MM-YY HH:MM:SS
        date_match = re.search(r'on (\d{2}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', body)
        
        # Merchant pattern: at {merchant} on
        merchant_match = re.search(r'at (.*?) on', body)
        
        if not (amt_match and card_match and date_match):
            return None

        amount = normalize_amount(float(amt_match.group(1).replace(",", "")))
        direction = infer_direction(
            subject=subject, body=body, payment_type="AXIS_CC_DEBIT"
        )
        return {
            "type": "AXIS_CC_DEBIT" if direction == "debit" else "AXIS_CC_CREDIT",
            "amount": amount,
            "direction": direction,
            "instrument_last4": card_match.group(1),
            "merchant": merchant_match.group(1).strip() if merchant_match else "Unknown",
            "timestamp": datetime.strptime(date_match.group(1), "%d-%m-%y %H:%M:%S"),
            "raw_merchant": merchant_match.group(1).strip() if merchant_match else "",
        }

if __name__ == "__main__":
    # Test case
    parser = AxisParser()
    test_body = "Thank you for using your Card no. XX8248 for INR 2420.3 at BAG2BAG TRA on 28-06-24 11:45:42."
    print("Axis Template:", parser.parse("Transaction alert", test_body))
