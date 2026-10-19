"""Every routing decision, one JSON line each — training data for the Phase 14 classifier.

%LOCALAPPDATA%\\JarvisAgent\\logs\\routes.jsonl · text is redacted before it's written.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from jarvis.paths import data_dir
from jarvis.safety.redact import redact
from jarvis.telemetry.logging import get_logger

log = get_logger(__name__)


def routes_path() -> Path:
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path / "routes.jsonl"


class RouteLog:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path

    def write(self, text: str, **fields: Any) -> None:
        record = {"ts": round(time.time(), 3), "text": redact(text), **fields}
        try:
            with (self.path or routes_path()).open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:  # logging must never break a command
            log.debug("route log write failed", exc_info=True)
