"""Spoken times for video control: "10 min" → 600, "1:02:03" → 3723, 754 → "12:34"."""

from __future__ import annotations

import re

_UNIT_SECONDS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
}  # fmt: skip
_CLOCK = re.compile(r"^\d+(?::\d{1,2}){1,2}$")


def to_seconds(amount: str, unit: str) -> float | None:
    """("10", "min") → 600. None if the amount or unit isn't understood."""
    factor = _UNIT_SECONDS.get(unit.strip().lower())
    if factor is None or not amount.strip().isdigit():
        return None
    return float(int(amount) * factor)


def parse_clock(text: str) -> float | None:
    """ "5:30" → 330, "1:02:03" → 3723. None if it isn't a clock time."""
    text = text.strip()
    if not _CLOCK.match(text):
        return None
    seconds = 0
    for part in text.split(":"):
        seconds = seconds * 60 + int(part)
    return float(seconds)


def fmt_clock(seconds: float) -> str:
    """754 → "12:34", 3723 → "1:02:03"."""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
