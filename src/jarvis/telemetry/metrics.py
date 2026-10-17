"""Latency measurement per pipeline stage (speed is the #1 priority)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from jarvis.telemetry.logging import get_logger

log = get_logger(__name__)


@dataclass
class Timing:
    stage: str
    ms: float = 0.0


@contextmanager
def timed(stage: str) -> Iterator[Timing]:
    """Measure a block and log its duration in ms.

    with timed("router") as t:
        ...
    t.ms  # elapsed milliseconds
    """
    timing = Timing(stage)
    start = time.perf_counter()
    try:
        yield timing
    finally:
        timing.ms = (time.perf_counter() - start) * 1000
        log.debug("stage=%s ms=%.1f", stage, timing.ms)
