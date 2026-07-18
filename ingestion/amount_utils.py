"""Helpers for debit/credit direction and amount normalization."""

from __future__ import annotations

import re

# Money leaving the account / card (increases spend)
_DEBIT_HINTS = re.compile(
    r"\b(debited|debit|spent|paid|purchase|payment was made|withdrawn)\b",
    re.I,
)
# Money returning (reduces net spend)
_CREDIT_HINTS = re.compile(
    r"\b(credited|credit|refund|reversed|reversal|cashback|chargeback|"
    r"payment received|received in)\b",
    re.I,
)


def normalize_amount(value: float) -> float:
    """Store magnitude only; direction is separate."""
    return abs(float(value))


def infer_direction(subject: str = "", body: str = "", payment_type: str | None = None) -> str:
    """
    Infer debit vs credit from email text / payment type label.

    Default is debit (spend). Explicit credit/refund language wins when present.
    """
    blob = f"{subject} {body} {payment_type or ''}"
    # Strong credit signals first
    if _CREDIT_HINTS.search(blob) and not re.search(r"\bcredit card\b", blob, re.I):
        # Avoid treating "Credit Card" as a credit/refund
        if re.search(r"\b(refund|reversed|reversal|cashback|chargeback|credited)\b", blob, re.I):
            return "credit"
        if re.search(r"\bcredited\b", blob, re.I):
            return "credit"
    if re.search(r"\b(refund|reversed|reversal|cashback|chargeback)\b", blob, re.I):
        return "credit"
    if payment_type and re.search(r"credit|refund", payment_type, re.I) and not re.search(
        r"cc|card", payment_type, re.I
    ):
        return "credit"
    if _DEBIT_HINTS.search(blob):
        return "debit"
    return "debit"


def coerce_direction(direction: str | None) -> str:
    d = (direction or "debit").strip().lower()
    if d not in ("debit", "credit"):
        raise ValueError("direction must be 'debit' or 'credit'")
    return d
