import time

from jarvis.telemetry.metrics import timed


def test_timed_measures_ms() -> None:
    with timed("unit") as t:
        time.sleep(0.02)
    assert t.stage == "unit"
    assert 15 <= t.ms < 500


def test_timed_records_even_on_error() -> None:
    try:
        with timed("boom") as t:
            raise ValueError
    except ValueError:
        pass
    assert t.ms >= 0
