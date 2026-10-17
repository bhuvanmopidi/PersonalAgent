import asyncio
import threading
from collections.abc import Callable

import pytest

from jarvis.safety.killswitch import Killed, KillSwitch, Listener


class FakeListener:
    """Stands in for the global keyboard hook; tests 'press' the key via press()."""

    instances: list["FakeListener"] = []

    def __init__(self, key: str, on_trigger: Callable[[], None]) -> None:
        self.key, self.press = key, on_trigger
        self.started = self.stopped = False
        FakeListener.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


def fake_factory(key: str, on_trigger: Callable[[], None]) -> Listener:
    return FakeListener(key, on_trigger)


async def test_normal_completion_returns_value() -> None:
    kill = KillSwitch("esc", fake_factory)

    async def work() -> int:
        return 42

    assert await kill.guard(work()) == 42
    listener = FakeListener.instances[-1]
    assert listener.started and listener.stopped  # hook only active during the task


async def test_key_press_from_other_thread_kills_task() -> None:
    kill = KillSwitch("esc", fake_factory)
    finished = False

    async def slow() -> None:
        nonlocal finished
        await asyncio.sleep(5)
        finished = True

    async def press_soon() -> None:
        await asyncio.sleep(0.05)
        threading.Thread(target=FakeListener.instances[-1].press).start()  # like pynput

    asyncio.get_running_loop().create_task(press_soon())
    with pytest.raises(Killed):
        await kill.guard(slow())
    assert not finished
    assert FakeListener.instances[-1].stopped


async def test_trigger_when_idle_is_harmless() -> None:
    KillSwitch("esc", fake_factory).trigger()
