import pytest

from jarvis.apps.media_time import fmt_clock, parse_clock, to_seconds


@pytest.mark.parametrize(
    ("amount", "unit", "seconds"),
    [
        ("10", "min", 600),
        ("30", "seconds", 30),
        ("2", "minutes", 120),
        ("1", "h", 3600),
        ("45", "s", 45),
        ("1", "hour", 3600),
    ],  # fmt: skip
)
def test_to_seconds(amount: str, unit: str, seconds: float) -> None:
    assert to_seconds(amount, unit) == seconds


@pytest.mark.parametrize(("amount", "unit"), [("ten", "min"), ("10", "days"), ("", "")])
def test_to_seconds_rejects(amount: str, unit: str) -> None:
    assert to_seconds(amount, unit) is None


@pytest.mark.parametrize(
    ("text", "seconds"), [("5:30", 330), ("1:02:03", 3723), ("0:00", 0), ("12:5", 725)]
)
def test_parse_clock(text: str, seconds: float) -> None:
    assert parse_clock(text) == seconds


@pytest.mark.parametrize("text", ["5", "abc", "1:2:3:4", ""])
def test_parse_clock_rejects(text: str) -> None:
    assert parse_clock(text) is None


@pytest.mark.parametrize(
    ("seconds", "text"), [(754, "12:34"), (3723, "1:02:03"), (5, "0:05"), (-3, "0:00")]
)
def test_fmt_clock(seconds: float, text: str) -> None:
    assert fmt_clock(seconds) == text
