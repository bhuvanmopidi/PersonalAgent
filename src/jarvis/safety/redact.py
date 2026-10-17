"""Mask sensitive data before it is logged or stored.

Emails and phone numbers are kept on purpose — the agent needs them (contacts, read-backs).
"""

from __future__ import annotations

import logging
import re

MASK = "[REDACTED]"

_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_SECRET_AFTER_WORD = re.compile(
    r"(?i)\b(otp|one[- ]time password|upi pin|pin|cvv|cvc)\b(\s*(?:is|:|=|-)?\s*)(\d{3,8})\b"
)
_PASSWORD = re.compile(r"(?i)\b(password|passwd|pwd)\b(\s*(?:is|:|=)\s*)(\S+)")
# exactly 12 digits — not part of a longer digit group (e.g. a 16-digit non-card number)
_AADHAAR = re.compile(r"(?<!\d)(?<!\d[ -])\d{4}[ -]?\d{4}[ -]?\d{4}(?![ -]?\d)")
_PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")


def luhn_valid(digits: str) -> bool:
    """Checksum used by all payment cards — avoids masking random long numbers."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _mask_card(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group())
    return "[CARD]" if 13 <= len(digits) <= 19 and luhn_valid(digits) else match.group()


def redact(text: str) -> str:
    text = _CARD.sub(_mask_card, text)
    text = _SECRET_AFTER_WORD.sub(rf"\1\2{MASK}", text)
    text = _PASSWORD.sub(rf"\1\2{MASK}", text)
    text = _AADHAAR.sub("[AADHAAR]", text)
    text = _PAN.sub("[PAN]", text)
    return text


class RedactingFilter(logging.Filter):
    """Attach to log handlers so every log line is redacted automatically."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        return True
