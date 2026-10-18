"""Global kill hotkey: pressing it cancels the running task instantly.

The key is only hooked while a task is running, so normal Esc use in apps is unaffected.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Coroutine
from typing import Any, Protocol, TypeVar

T = TypeVar("T")


class Killed(Exception):
    """Raised when the user pressed the kill hotkey during a task."""


class Listener(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...


ListenerFactory = Callable[[str, Callable[[], None]], Listener]


def pynput_listener(key_name: str, on_trigger: Callable[[], None]) -> Listener:
    """Global keyboard hook (runs in its own thread)."""
    from pynput import keyboard

    target = keyboard.Key[key_name]

    def on_press(key: keyboard.Key | keyboard.KeyCode | None, injected: bool = False) -> None:
        if key == target and not injected:  # ignore keys the agent itself sends
            on_trigger()

    return keyboard.Listener(on_press=on_press)


class KillSwitch:
    def __init__(self, key: str = "esc", listener_factory: ListenerFactory = pynput_listener):
        self.key = key
        self._factory = listener_factory
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[Any] | None = None
        self._killed = False
        self._killed_by = key

    async def guard(self, coro: Coroutine[Any, Any, T]) -> T:
        """Run coro; if the kill key is pressed meanwhile, cancel it and raise Killed."""
        if self._task is not None:
            raise RuntimeError("KillSwitch is already guarding a task")
        self._loop = asyncio.get_running_loop()
        task: asyncio.Task[T] = asyncio.create_task(coro)
        self._task = task
        self._killed = False
        listener = self._factory(self.key, self.trigger)
        listener.start()
        try:
            return await task
        except asyncio.CancelledError:
            if self._killed:
                raise Killed(f"stopped by {self._killed_by}") from None
            raise
        finally:
            listener.stop()
            self._task = None
            self._loop = None

    @property
    def active(self) -> bool:
        return self._task is not None

    def trigger(self, by: str | None = None) -> None:
        """Thread-safe: called from the keyboard-hook thread, or by voice ("stop")."""
        loop, task = self._loop, self._task
        if loop is None or task is None:
            return
        self._killed = True
        self._killed_by = by or f"'{self.key}'"
        with contextlib.suppress(RuntimeError):  # loop already closed
            loop.call_soon_threadsafe(task.cancel)
