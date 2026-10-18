"""Known folders (Downloads, Documents, …) and a fast, bounded file search."""

from __future__ import annotations

import os
import subprocess
import time
import winreg
from pathlib import Path

from rapidfuzz import fuzz

_SHELL_FOLDERS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
# Registry value names. They respect OneDrive / moved folders, unlike Path.home() / "Documents".
_KNOWN = {
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "documents": "Personal",
    "desktop": "Desktop",
    "pictures": "My Pictures",
    "photos": "My Pictures",
    "music": "My Music",
    "videos": "My Video",
}
_SKIP_DIRS = {"node_modules", "__pycache__", "venv", ".venv", "appdata", "$recycle.bin", ".git"}
MATCH_CUTOFF = 80


def known_folder(name: str) -> Path | None:
    name = name.lower().strip()
    if name == "home":
        return Path.home()
    value_name = _KNOWN.get(name)
    if value_name is None:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _SHELL_FOLDERS_KEY) as key:
            raw, _ = winreg.QueryValueEx(key, value_name)
        return Path(os.path.expandvars(raw))
    except OSError:
        return Path.home() / name.capitalize()


def search_roots() -> list[Path]:
    roots = [known_folder(n) for n in ("desktop", "documents", "downloads")]
    return [r for r in roots if r and r.exists()]


def find_files(
    query: str,
    roots: list[Path],
    limit: int = 5,
    max_depth: int = 5,
    time_budget_s: float = 1.5,
) -> list[Path]:
    """Fuzzy filename search. Best score first, newest first on ties. Bounded by depth and time."""
    q = query.lower().strip()
    deadline = time.monotonic() + time_budget_s
    scored: list[tuple[float, float, Path]] = []

    for root in roots:
        base_depth = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root):
            if time.monotonic() > deadline:
                break
            depth = len(Path(dirpath).parts) - base_depth
            dirnames[:] = [
                d
                for d in dirnames
                if depth < max_depth and not d.startswith(".") and d.lower() not in _SKIP_DIRS
            ]
            for fname in filenames:
                score = fuzz.partial_ratio(q, Path(fname).stem.lower())
                if score >= MATCH_CUTOFF:
                    path = Path(dirpath) / fname
                    try:
                        mtime = path.stat().st_mtime
                    except OSError:
                        continue
                    scored.append((score, mtime, path))

    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    return [p for _, _, p in scored[:limit]]


def reveal_in_explorer(path: Path) -> None:
    """Open Explorer with the file selected."""
    subprocess.Popen(["explorer.exe", f"/select,{path}"])


class FileTools:
    def known_folder(self, name: str) -> Path | None:
        return known_folder(name)

    def find(self, query: str) -> list[Path]:
        return find_files(query, search_roots())

    def reveal(self, path: Path) -> None:
        reveal_in_explorer(path)
