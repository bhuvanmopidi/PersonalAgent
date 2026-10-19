"""Index of installed apps (everything in the Start menu), cached on disk.

`Get-StartApps` lists both classic and Store apps with an AppID; every AppID launches the same way
via `shell:AppsFolder\\<AppID>`, so one index covers all apps.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, TypeAdapter
from rapidfuzz import fuzz, process

from jarvis.paths import cache_dir
from jarvis.telemetry.logging import get_logger

log = get_logger(__name__)

FUZZY_CUTOFF = 85
CONTAINED_CUTOFF = 90  # a short form must sit (almost) whole inside the name
_SKIP_WORDS = ("uninstall", "readme", "release notes", "documentation")


Quality = Literal["exact", "prefix", "fuzzy"]


class AppEntry(BaseModel):
    name: str
    app_id: str

    @property
    def is_web_app(self) -> bool:
        """An installed browser web app (Chrome/Edge PWA, e.g. YouTube, Gmail). It's a website
        running in your normal browser profile — Jarvis opens the site in its own window instead."""
        return "_crx_" in self.app_id


def best_match(
    query: str, names: list[str], scorer: Callable[..., float] = fuzz.WRatio
) -> tuple[str, Quality] | None:
    """Exact name → shortest name starting with query → fuzzy match (typos, partial names).

    `names` must be lowercase. Returns the matched name and how good the match was.
    WRatio also scores partial matches ("chrome" → "google chrome"); pass fuzz.ratio for
    whole-string typo matching only.
    """
    q = query.lower().strip()
    if not q:
        return None
    if q in names:
        return q, "exact"
    prefixed = [n for n in names if n.startswith(q)]
    if prefixed:
        return min(prefixed, key=len), "prefix"
    for name, _, _ in process.extract(q, names, scorer=scorer, score_cutoff=FUZZY_CUTOFF, limit=5):
        if looks_like(q, name):
            return name, "fuzzy"
    return None


def looks_like(query: str, name: str) -> bool:
    """A fuzzy match is only for typos ("spotfy" → spotify) or short forms that are almost entirely
    inside the name ("chrome" → google chrome) — never for a phrase that merely shares a word with
    it ("latest mr beast video" is not "What is new in the latest version", "mr beast channel in
    youtube" is not YouTube)."""
    if fuzz.ratio(query, name) >= FUZZY_CUTOFF:
        return True
    return len(query) <= len(name) and fuzz.partial_ratio(query, name) >= CONTAINED_CUTOFF


_ENTRIES = TypeAdapter(list[AppEntry])


def query_start_apps() -> list[AppEntry]:
    """Ask Windows for every Start-menu app (~1 s, so results are cached)."""
    script = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
        "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"
    )
    out = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    ).stdout
    raw = json.loads(out or "[]")
    rows = raw if isinstance(raw, list) else [raw]
    return [
        AppEntry(name=r["Name"], app_id=r["AppID"])
        for r in rows
        if r.get("Name") and r.get("AppID") and not any(w in r["Name"].lower() for w in _SKIP_WORDS)
    ]


class AppIndex:
    def __init__(self, entries: list[AppEntry]) -> None:
        self.entries = entries
        self._by_name = {e.name.lower(): e for e in entries}
        self._names = list(self._by_name)

    @classmethod
    def load(cls, cache_path: Path | None = None, refresh: bool = False) -> AppIndex:
        path = cache_path or cache_dir() / "app_index.json"
        if path.exists() and not refresh:
            return cls(_ENTRIES.validate_json(path.read_bytes()))
        entries = query_start_apps()
        path.write_bytes(_ENTRIES.dump_json(entries))
        log.info("app index built: %d apps", len(entries))
        return cls(entries)

    def find(self, query: str) -> AppEntry | None:
        found = self.find_scored(query)
        return found[0] if found else None

    def find_scored(self, query: str) -> tuple[AppEntry, Quality] | None:
        match = best_match(query, self._names)
        return (self._by_name[match[0]], match[1]) if match else None
