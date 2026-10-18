"""Local data locations (%LOCALAPPDATA%\\JarvisAgent)."""

from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    path = base / "JarvisAgent"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir() -> Path:
    path = data_dir() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path
