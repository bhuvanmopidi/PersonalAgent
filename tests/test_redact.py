import logging

import pytest

from jarvis.safety.redact import RedactingFilter, luhn_valid, redact


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("card 4111 1111 1111 1111 ok", "card [CARD] ok"),
        ("card 4111-1111-1111-1111", "card [CARD]"),
        ("your OTP is 482913", "your OTP is [REDACTED]"),
        ("OTP: 4829", "OTP: [REDACTED]"),
        ("upi pin 123456", "upi pin [REDACTED]"),
        ("CVV 123", "CVV [REDACTED]"),
        ("password: hunter2!", "password: [REDACTED]"),
        ("aadhaar 1234 5678 9012", "aadhaar [AADHAAR]"),
        ("PAN ABCDE1234F", "PAN [PAN]"),
    ],
)
def test_masks_sensitive(text: str, expected: str) -> None:
    assert redact(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "mail arjun@gmail.com",  # emails kept
        "call 9876543210",  # phone numbers kept
        "order 4111 1111 1111 1112",  # fails Luhn → not a card
        "took 312 ms",
    ],
)
def test_keeps_normal_text(text: str) -> None:
    assert redact(text) == text


def test_luhn() -> None:
    assert luhn_valid("4111111111111111")
    assert not luhn_valid("4111111111111112")


def test_logging_filter_redacts_args() -> None:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "otp is %s", ("482913",), None)
    RedactingFilter().filter(record)
    assert record.getMessage() == "otp is [REDACTED]"
