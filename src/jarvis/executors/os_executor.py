"""Executes Tier-1 OS actions. Only ever called through ActionRunner."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from jarvis.models import Action, ActionResult
from jarvis.tiers.instant.apps import LaunchPlan


class Launcher(Protocol):
    def resolve(self, query: str) -> LaunchPlan | None: ...
    def launch(self, plan: LaunchPlan) -> None: ...
    def close(self, query: str) -> list[str]: ...


class Level(Protocol):
    """Anything with a 0–100 level: volume, brightness."""

    def get(self) -> int: ...
    def set(self, percent: int) -> None: ...


class VolumeControl(Level, Protocol):
    def set_mute(self, muted: bool) -> None: ...


class Files(Protocol):
    def known_folder(self, name: str) -> Path | None: ...
    def find(self, query: str) -> list[Path]: ...
    def reveal(self, path: Path) -> None: ...


def _int(value: object, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _new_level(current: int, mode: str, value: int) -> int:
    if mode == "up":
        return min(100, current + value)
    if mode == "down":
        return max(0, current - value)
    return max(0, min(100, value))  # "set"


def ok(observation: str) -> ActionResult:
    return ActionResult(ok=True, observation=observation)


def fail(error: str, observation: str) -> ActionResult:
    return ActionResult(ok=False, error=error, observation=observation)


class OSExecutor:
    def __init__(
        self,
        launcher: Launcher,
        volume: VolumeControl,
        brightness: Level,
        files: Files,
        opener: Callable[[str], None] = os.startfile,
        locker: Callable[[], None] | None = None,
    ) -> None:
        from jarvis.tiers.instant.system import lock_screen

        self.launcher, self.volume, self.brightness, self.files = (
            launcher,
            volume,
            brightness,
            files,
        )
        self._open = opener
        self._lock = locker or lock_screen
        self._handlers: dict[str, Callable[[Action], Awaitable[ActionResult]]] = {
            "launch_app": self._launch_app,
            "close_app": self._close_app,
            "open_url": self._open_url,
            "set_volume": self._set_volume,
            "set_brightness": self._set_brightness,
            "open_folder": self._open_folder,
            "find_file": self._find_file,
            "lock_screen": self._lock_screen,
        }

    async def execute(self, action: Action) -> ActionResult:
        handler = self._handlers.get(action.kind)
        if handler is None:
            return fail("unsupported", f"OS executor can't do '{action.kind}'")
        return await handler(action)

    async def _launch_app(self, action: Action) -> ActionResult:
        # resolve may rebuild the app index (~1 s) — keep it off the event loop
        plan = await asyncio.to_thread(self.launcher.resolve, action.target)
        if plan is None:
            return fail("not_found", f"I couldn't find an app or site called '{action.target}'")
        self.launcher.launch(plan)
        return ok(f"Opened {plan.display}")

    async def _close_app(self, action: Action) -> ActionResult:
        try:
            closed = await asyncio.to_thread(self.launcher.close, action.target)
        except PermissionError as exc:
            return fail("protected", str(exc))
        if not closed:
            return fail("not_running", f"'{action.target}' doesn't seem to be running")
        return ok(f"Closed {', '.join(closed)}")

    async def _open_url(self, action: Action) -> ActionResult:
        self._open(action.target)
        return ok(f"Opened {action.target}")

    async def _set_volume(self, action: Action) -> ActionResult:
        mode = str(action.args.get("mode", "set"))
        if mode in ("mute", "unmute"):
            self.volume.set_mute(mode == "mute")
            return ok("Muted" if mode == "mute" else "Unmuted")
        level = _new_level(self.volume.get(), mode, _int(action.args.get("value"), 10))
        self.volume.set(level)
        self.volume.set_mute(False)  # "volume up" while muted should be audible
        return ok(f"Volume {level}%")

    async def _set_brightness(self, action: Action) -> ActionResult:
        mode = str(action.args.get("mode", "set"))
        try:
            level = _new_level(self.brightness.get(), mode, _int(action.args.get("value"), 10))
            self.brightness.set(level)
        except Exception as exc:  # e.g. external monitor without DDC/CI
            return fail("unsupported_display", f"Couldn't change brightness: {exc}")
        return ok(f"Brightness {level}%")

    async def _open_folder(self, action: Action) -> ActionResult:
        path = self.files.known_folder(action.target)
        if path is None or not path.exists():
            return fail("not_found", f"I don't know a folder called '{action.target}'")
        self._open(str(path))
        return ok(f"Opened {path}")

    async def _find_file(self, action: Action) -> ActionResult:
        query = str(action.args.get("query", action.target))
        matches = await asyncio.to_thread(self.files.find, query)
        if not matches:
            return fail("not_found", f"No file matching '{query}' in Desktop, Documents, Downloads")
        self.files.reveal(matches[0])
        more = f" (+{len(matches) - 1} more)" if len(matches) > 1 else ""
        return ok(f"Found {matches[0]}{more}")

    async def _lock_screen(self, action: Action) -> ActionResult:
        self._lock()
        return ok("Locked")
