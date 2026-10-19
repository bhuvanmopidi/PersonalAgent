"""Files in and out (Phase 11.3), for the general agent on any website or app.

attach_file   which file? — a full path, or a description ("my resume") searched in the user's
              folders. Resolved BEFORE the safety check (`FileResolver.annotate`), so the policy
              judges the real file (blocked paths, programs) and the confirmation can name it.
              The browser / desktop executors then do the attaching itself.
wait_download a new, finished file in the downloads folder
open_file     open a file in its own app (a downloaded program only after a yes — policy)
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from jarvis.models import Action, ActionResult
from jarvis.tiers.instant.files import find_files, known_folder, search_roots

FILE_KINDS = frozenset({"attach_file", "open_file", "wait_download"})
RESOLVED_KINDS = frozenset({"attach_file", "open_file"})  # these name a file: find it first
DOWNLOAD_WAIT_S = 30.0
_PARTIAL = (".crdownload", ".tmp", ".part", ".partial", ".download")
# words that describe the file but aren't part of its name
_FILLER = {"my", "the", "a", "an", "file", "document", "doc", "this", "that", "latest", "newest",
           "recent", "last", "from", "in", "on"}  # fmt: skip
_FOLDER_WORDS = ("downloads", "desktop", "documents", "pictures", "music", "videos")
_NEWEST = re.compile(r"\b(latest|newest|recent|last)\b", re.I)

Annotator = Callable[[Action], Awaitable[Action]]


def size_text(path: Path) -> str:
    kb = max(1, path.stat().st_size // 1024)
    return f"{kb} KB" if kb < 1024 else f"{kb / 1024:.1f} MB"


def _looks_like_path(text: str) -> bool:
    return bool(re.match(r"^([a-zA-Z]:[\\/]|\\\\|~[\\/]|\.{1,2}[\\/])", text))


def resolve_file(text: str, roots: list[Path] | None = None) -> tuple[Path | None, str]:
    """(the file, "") — or (None, why not): no such file, or several match (the user picks)."""
    text = text.strip().strip('"').strip()
    if not text:
        return None, "which file? (a name or a full path)"
    if _looks_like_path(text):
        path = Path(os.path.expanduser(text))
        if path.is_file():
            return path, ""
        same_name = sorted(path.parent.glob(path.name + ".*")) if not path.suffix else []
        if len(same_name) == 1:
            return same_name[0], ""
        return None, f"{path} doesn't exist"
    named = _by_exact_name(text, roots)
    if named is not None:
        return named
    words = [w for w in re.findall(r"[\w.\-]+", text.lower()) if w not in _FILLER]
    query = " ".join(words) or text
    found = find_files(query, roots if roots is not None else search_roots(), limit=8)
    found = [p for p in found if p.is_file()]
    if not found:
        return None, f'no file like "{text}" in your folders'
    if len(found) == 1:
        return found[0], ""
    exact = [p for p in found if p.name.lower() == query or p.stem.lower() == query]
    if len(exact) == 1:
        return exact[0], ""
    if _NEWEST.search(text):  # "my latest photo": the newest of the matches
        return max(found, key=lambda p: p.stat().st_mtime), ""
    names = ", ".join(str(p) for p in found[:5])
    return None, f"several files match — ask the user which one: {names}"


def _by_exact_name(text: str, roots: list[Path] | None) -> tuple[Path | None, str] | None:
    """A file name WITH its extension ("Resume 8.pdf", "Downloads\\Resume 8.pdf") is exact: that
    file, not a fuzzy search. A leading folder word ("Downloads") means the user's real folder.
    None = the text isn't an exact name (search by description instead)."""
    low = text.lower()
    if not re.search(r"\.[a-z0-9]{1,5}\b", low):
        return None  # no extension anywhere: a description
    # a folder word anywhere ("… in Downloads", "Desktop\…"): one of the given roots by that name
    # (evals, tests), else the user's real folder
    words = set(re.findall(r"[a-z]+", low))
    hint = next((f for f in _FOLDER_WORDS if f in words), "")
    folder = next((r for r in roots or [] if r.name.lower() == hint), None) if hint else None
    folder = folder or (known_folder(hint) if hint else None)
    search = [folder] if folder is not None else (roots if roots is not None else search_roots())
    query = " ".join(w for w in re.findall(r"[\w.\-]+", low)
                     if w not in _FILLER and w not in _FOLDER_WORDS)  # fmt: skip
    # the files whose FULL name is written in the request ("…Resume 8.pdf in Downloads")
    same = [p for p in find_files(query, search, limit=30) if p.is_file() and p.name.lower() in low]
    if same:
        longest = max(len(p.name) for p in same)  # "Resume 8.pdf" over "8.pdf"
        same = [p for p in same if len(p.name) == longest]
    if len(same) == 1:
        return same[0], ""
    if len(same) > 1:
        names = ", ".join(str(p) for p in same[:5])
        return None, f"several files are called {same[0].name} — ask the user which one: {names}"
    return None  # no file with exactly that name: try it as a description


class FileResolver:
    """ActionRunner annotator: turns attach / open requests into a real path (or a reason)."""

    def __init__(self, roots: list[Path] | None = None) -> None:
        self.roots = roots  # None = the user's folders (evals use a test folder)

    async def annotate(self, action: Action) -> Action:
        if action.kind not in RESOLVED_KINDS or action.args.get("path"):
            return action
        path, problem = await asyncio.to_thread(resolve_file, action.target, self.roots)
        if path is None:
            return action.model_copy(update={"args": {**action.args, "path_problem": problem}})
        return action.model_copy(
            update={"target": str(path), "args": {**action.args, "path": str(path)}}
        )


def chain(*annotators: Annotator | None) -> Annotator:
    """Several annotators, in order (e.g. the page URL, then the file)."""
    steps = [a for a in annotators if a is not None]

    async def run(action: Action) -> Action:
        for step in steps:
            action = await step(action)
        return action

    return run


def resolved(action: Action) -> tuple[Path | None, ActionResult | None]:
    """The file an attach / open action is about — or the failure to report instead."""
    problem = action.args.get("path_problem")
    if problem:
        error = "ambiguous_file" if "several" in str(problem) else "no_file"
        return None, ActionResult(ok=False, error=error, observation=str(problem))
    path = Path(str(action.args.get("path") or action.target))
    if not path.is_file():
        return None, ActionResult(ok=False, error="no_file", observation=f"{path} doesn't exist")
    return path, None


def attached(path: Path) -> ActionResult:
    return ActionResult(ok=True, observation=f"Attached {path.name} ({size_text(path)})",
                        data={"attached": str(path)})  # fmt: skip


def downloads_folder() -> Path:
    return known_folder("downloads") or Path.home() / "Downloads"


async def wait_for_download(
    folder: Path, since: float, timeout_s: float = DOWNLOAD_WAIT_S, known: list[Path] | None = None
) -> ActionResult:
    """A new, finished download: first one the browser told us about (`known`), else a file
    that appeared in the folder since `since` and stopped growing."""
    deadline = time.monotonic() + timeout_s
    sizes: dict[Path, int] = {}
    while time.monotonic() < deadline:
        for path in list(known or []):
            if path.is_file():
                return ActionResult(ok=True, observation=f"Downloaded {path} ({size_text(path)})",
                                    data={"downloaded": str(path)})  # fmt: skip
        try:
            fresh = [p for p in folder.iterdir()
                     if p.is_file() and p.stat().st_mtime >= since - 1]  # fmt: skip
        except OSError:
            fresh = []
        busy = any(p.name.lower().endswith(_PARTIAL) for p in fresh)
        done = [p for p in fresh if not p.name.lower().endswith(_PARTIAL)]
        for path in sorted(done, key=lambda p: p.stat().st_mtime, reverse=True):
            size = path.stat().st_size
            if not busy and sizes.get(path) == size and size > 0:  # stopped growing
                return ActionResult(ok=True, observation=f"Downloaded {path} ({size_text(path)})",
                                    data={"downloaded": str(path)})  # fmt: skip
            sizes[path] = size
        await asyncio.sleep(0.5)
    why = f"No download finished in {folder} in {timeout_s:.0f} s"
    return ActionResult(ok=False, error="no_download", observation=why)


async def open_file(action: Action, opener: Callable[[str], None] = os.startfile) -> ActionResult:
    path, failure = resolved(action)
    if failure is not None or path is None:
        return failure or ActionResult(ok=False, error="no_file")
    await asyncio.to_thread(opener, str(path))
    return ActionResult(ok=True, observation=f"Opened {path.name}")
