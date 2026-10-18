"""Index of installed apps (everything in the Start menu), cached on disk.

`Get-StartApps` lists both classic and Store apps with an AppID; every AppID launches the same way
via `shell:AppsFolder\\<AppID>`, so one index covers all apps.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from pydantic import BaseModel, TypeAdapter
from rapidfuzz import fuzz, process

from jarvis.paths import cache_dir
from jarvis.telemetry.logging import get_logger

log = get_logger(__name__)

FUZZY_CUTOFF = 85
_SKIP_WORDS = ("uninstall", "readme", "release notes", "documentation")


class AppEntry(BaseModel):
    name: str
    app_id: str


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
        """Exact name → shortest name starting with query → fuzzy match (typos, partial names)."""
        q = query.lower().strip()
        if not q:
            return None
        if q in self._by_name:
            return self._by_name[q]
        prefixed = [n for n in self._names if n.startswith(q)]
        if prefixed:
            return self._by_name[min(prefixed, key=len)]
        match = process.extractOne(q, self._names, scorer=fuzz.WRatio, score_cutoff=FUZZY_CUTOFF)
        return self._by_name[match[0]] if match else None
