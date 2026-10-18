from pathlib import Path

import pytest

from jarvis.executors.os_executor import OSExecutor
from jarvis.models import Action
from jarvis.tiers.instant.apps import LaunchPlan


class FakeLauncher:
    def __init__(self) -> None:
        self.launched: list[LaunchPlan] = []

    def resolve(self, query: str) -> LaunchPlan | None:
        return LaunchPlan(kind="app", value="id", display="Spotify") if query == "spotify" else None

    def launch(self, plan: LaunchPlan) -> None:
        self.launched.append(plan)

    def close(self, query: str) -> list[str]:
        if query == "explorer":
            raise PermissionError("protected")
        return ["Notepad.exe"] if query == "notepad" else []


class FakeLevel:
    def __init__(self, level: int = 50, broken: bool = False) -> None:
        self.level, self.muted, self.broken = level, True, broken

    def get(self) -> int:
        if self.broken:
            raise RuntimeError("no DDC/CI")
        return self.level

    def set(self, percent: int) -> None:
        self.level = percent

    def set_mute(self, muted: bool) -> None:
        self.muted = muted


class FakeFiles:
    def __init__(self, matches: list[Path]) -> None:
        self.matches = matches
        self.revealed: Path | None = None

    def known_folder(self, name: str) -> Path | None:
        return Path.home() if name == "home" else None

    def find(self, query: str) -> list[Path]:
        return self.matches

    def reveal(self, path: Path) -> None:
        self.revealed = path


def make(
    volume: int = 50, broken_brightness: bool = False, matches: list[Path] | None = None
) -> tuple[OSExecutor, FakeLauncher, FakeLevel, list[str]]:
    launcher, vol = FakeLauncher(), FakeLevel(volume)
    opened: list[str] = []
    executor = OSExecutor(
        launcher,
        vol,
        FakeLevel(70, broken_brightness),
        FakeFiles(matches or []),
        opener=opened.append,
        locker=lambda: None,
    )
    return executor, launcher, vol, opened


async def test_launch_found_and_not_found() -> None:
    executor, launcher, _, _ = make()
    assert (await executor.execute(Action(kind="launch_app", target="spotify"))).ok
    assert launcher.launched[0].display == "Spotify"
    missing = await executor.execute(Action(kind="launch_app", target="photoshop"))
    assert missing.error == "not_found" and "photoshop" in missing.observation


async def test_close() -> None:
    executor, *_ = make()
    assert (await executor.execute(Action(kind="close_app", target="notepad"))).ok
    assert (await executor.execute(Action(kind="close_app", target="x"))).error == "not_running"
    assert (
        await executor.execute(Action(kind="close_app", target="explorer"))
    ).error == "protected"


@pytest.mark.parametrize(
    ("mode", "value", "start", "expected"),
    [("up", "10", 50, 60), ("up", "20", 95, 100), ("down", "10", 5, 0), ("set", "30", 80, 30)],
)
async def test_volume_levels(mode: str, value: str, start: int, expected: int) -> None:
    executor, _, vol, _ = make(volume=start)
    result = await executor.execute(Action(kind="set_volume", args={"mode": mode, "value": value}))
    assert result.ok and vol.level == expected and result.observation == f"Volume {expected}%"
    assert vol.muted is False  # changing volume also unmutes


async def test_mute_unmute() -> None:
    executor, _, vol, _ = make()
    await executor.execute(Action(kind="set_volume", args={"mode": "mute"}))
    assert vol.muted
    await executor.execute(Action(kind="set_volume", args={"mode": "unmute"}))
    assert not vol.muted


async def test_brightness_failure_is_reported() -> None:
    executor, *_ = make(broken_brightness=True)
    result = await executor.execute(Action(kind="set_brightness", args={"mode": "up"}))
    assert result.error == "unsupported_display"


async def test_open_url_and_folder() -> None:
    executor, _, _, opened = make()
    assert (await executor.execute(Action(kind="open_url", target="https://x.com"))).ok
    assert (await executor.execute(Action(kind="open_folder", target="home"))).ok
    assert opened == ["https://x.com", str(Path.home())]
    bad = await executor.execute(Action(kind="open_folder", target="nope"))
    assert bad.error == "not_found"


async def test_find_file_reveals_best() -> None:
    best = Path.home() / "resume.pdf"
    executor, *_ = make(matches=[best, Path.home() / "old.pdf"])
    result = await executor.execute(Action(kind="find_file", args={"query": "resume"}))
    assert result.ok and "resume.pdf" in result.observation and "+1 more" in result.observation


async def test_unsupported_kind() -> None:
    executor, *_ = make()
    assert (await executor.execute(Action(kind="pay"))).error == "unsupported"
