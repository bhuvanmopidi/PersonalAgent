"""Phase 11.2: the general agent in Windows apps (UI Automation).

Logic tests run everywhere. Tests marked `desktop` drive a real window — the predictable test app
evals/apps/testapp.ps1 — with the real mouse and keyboard: `uv run pytest -m desktop`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from jarvis.config import AgentSettings
from jarvis.executors.base import ActionRunner
from jarvis.executors.desktop import (
    DesktopExecutor,
    DesktopSession,
    Window,
    match_score,
    parse_keys,
)
from jarvis.executors.router import KindRouter
from jarvis.models import Action, ActionResult
from jarvis.safety.confirm import ScriptedConfirmer
from jarvis.safety.policy import Decision, SafetyPolicy
from jarvis.tiers.structured.agent import TaskAgent, to_action
from jarvis.tiers.structured.observer import Element, Snapshot
from jarvis.tiers.structured.prompts import DESKTOP_SYSTEM, RULES, SYSTEM
from jarvis.tiers.structured.schema import AgentAction, AgentStep
from jarvis.tiers.structured.surface import DesktopSurface

# ---- logic ---------------------------------------------------------------------------------------


def test_key_combos_are_parsed() -> None:
    assert parse_keys("Ctrl+L") == (["ctrl"], "l")
    assert parse_keys("ctrl + shift + n") == (["ctrl", "shift"], "n")
    assert parse_keys("Alt+Left") == (["alt"], "left")
    assert parse_keys("Enter") == ([], "enter")
    assert parse_keys("ArrowDown") == ([], "arrowdown")
    assert parse_keys("Hyper+Q") is None and parse_keys("") is None


def test_windows_match_app_names() -> None:
    spotify = Window(1, 1, "spotify", "Spotify Premium")
    calc = Window(2, 2, "calculatorapp", "Calculator")
    notepad = Window(3, 3, "notepad", "shopping list.txt - Notepad")
    assert match_score("spotify", spotify) == 100
    assert match_score("calculator", calc) == 100
    assert match_score("notepad", notepad) == 100
    assert match_score("spotify", notepad) < 80
    assert match_score("pad", notepad) < 100  # very short names only count as whole words


def snap_with(*elements: Element, url: str = "app://testapp") -> Snapshot:
    return Snapshot(url=url, title="Test", elements=list(elements))


def test_desktop_actions_are_routed_and_labelled() -> None:
    box = Element(id=1, tag="check box", name="Dark mode", checked=False)
    action, _ = to_action(AgentAction(op="click", element=1), snap_with(box), "desktop")
    assert action is not None and action.kind == "click" and action.target == "Dark mode"
    assert action.args["surface"] == "desktop" and "value_change" in action.flags

    opened, _ = to_action(AgentAction(op="open_app", text="Spotify"), snap_with(), "desktop")
    assert opened is not None and opened.kind == "launch_app" and opened.target == "Spotify"

    song = Element(id=2, tag="list item", name="Faded", checked=False)
    play, _ = to_action(AgentAction(op="double_click", element=2), snap_with(song), "desktop")
    assert play is not None and play.kind == "double_click" and "value_change" not in play.flags


def test_each_surface_only_does_its_own_actions() -> None:
    nav = AgentAction(op="navigate", url="https://example.com")
    action, problem = to_action(nav, snap_with(), "desktop")
    assert action is None and problem is not None and "can't be done here" in problem
    action, problem = to_action(AgentAction(op="open_app", text="Spotify"), snap_with(), "browser")
    assert action is None


def test_password_boxes_in_apps_are_handed_over() -> None:
    pw = Element(id=1, tag="edit", name="Password", password=True)
    action, _ = to_action(AgentAction(op="type", element=1, text="x"), snap_with(pw), "desktop")
    assert action is not None
    assert SafetyPolicy.from_yaml().check(action).decision == Decision.HANDOFF


def test_changing_a_windows_setting_is_confirmed_but_looking_around_is_free() -> None:
    policy = SafetyPolicy.from_yaml()
    switch = Element(id=1, tag="button", name="Dark mode", checked=False)  # a toggle switch
    page = Element(id=2, tag="list item", name="Personalization")
    settings = snap_with(switch, page, url="app://systemsettings")
    flip, _ = to_action(AgentAction(op="click", element=1), settings, "desktop")
    visit, _ = to_action(AgentAction(op="click", element=2), settings, "desktop")
    assert flip is not None and visit is not None
    assert policy.check(flip).decision == Decision.CONFIRM
    assert policy.check(visit).decision == Decision.ALLOW
    # the same switch in an ordinary app is just a click
    flip_elsewhere, _ = to_action(AgentAction(op="click", element=1), snap_with(switch), "desktop")
    assert flip_elsewhere is not None and policy.check(flip_elsewhere).decision == Decision.ALLOW


def test_keys_that_send_or_delete_for_good_are_confirmed() -> None:
    policy = SafetyPolicy.from_yaml()
    for key in ("Ctrl+Enter", "Shift+Delete", "shift + del"):
        assert (
            policy.check(Action(kind="press_key", args={"key": key})).decision == Decision.CONFIRM
        )
    assert policy.check(Action(kind="press_key", args={"key": "Ctrl+L"})).decision == Decision.ALLOW


def test_desktop_actions_go_to_the_desktop_executor() -> None:
    class Recorder:
        def __init__(self) -> None:
            self.seen: list[str] = []

        async def execute(self, action: Action) -> ActionResult:
            self.seen.append(action.kind)
            return ActionResult(ok=True)

    browser, desktop, default = Recorder(), Recorder(), Recorder()
    router = KindRouter(default, [(["click"], browser)], surfaces={"desktop": desktop})
    asyncio.run(router.execute(Action(kind="click", args={"surface": "desktop"})))
    asyncio.run(router.execute(Action(kind="click")))
    assert desktop.seen == ["click"] and browser.seen == ["click"]


def test_app_and_website_agents_share_the_same_rules() -> None:
    assert SYSTEM.endswith(RULES) and DESKTOP_SYSTEM.endswith(RULES)
    assert "open_app" in DESKTOP_SYSTEM and "navigate" not in DESKTOP_SYSTEM.split("Rules:")[0]


# ---- live: the test app --------------------------------------------------------------------------

desktop = pytest.mark.desktop


@pytest.fixture
async def app() -> AsyncIterator[tuple[DesktopSession, str]]:
    from jarvis.tiers.structured.evaluate import launch_test_app

    title = f"Jarvis Test {uuid.uuid4().hex[:6]}"
    proc = launch_test_app(title)
    session = DesktopSession()
    try:
        for _ in range(80):  # THIS test's window — not a look-alike left over from another run
            window = await session.switch(title)
            if window is not None and window.title == title:
                break
            session.window = None
            await asyncio.sleep(0.1)
        assert session.window is not None, "the test app didn't open"
        yield session, title
    finally:
        proc.kill()
        proc.wait(timeout=5)
        session.close()


def named(snap: Snapshot, name: str) -> int:
    return next(e.id for e in snap.elements if e.name == name)


class NoLauncher:
    def resolve(self, query: str, prefer: str = "auto") -> None:
        return None

    def launch(self, plan: object) -> None:
        raise AssertionError("nothing should be launched")


def runner_for(session: DesktopSession, answers: list[bool] | None = None) -> ActionRunner:
    confirmer = ScriptedConfirmer(answers or [])
    return ActionRunner(SafetyPolicy.from_yaml(), DesktopExecutor(session, NoLauncher()), confirmer)


async def act(runner: ActionRunner, snap: Snapshot, op: AgentAction) -> ActionResult:
    action, problem = to_action(op, snap, "desktop")
    assert action is not None, problem
    return await runner.run_action(action)


@desktop
async def test_observer_reads_the_app_like_a_page(app: tuple[DesktopSession, str]) -> None:
    session, title = app
    snap = await session.observe(120, 1500)
    names = {e.name: e for e in snap.elements}
    assert {"Your name", "Country", "Send me news", "Save", "Delete account", "Play"} <= set(names)
    assert names["Password"].password and names["Password"].value == ""
    assert names["Country"].value == "India" and names["Send me news"].checked is False
    assert "Blinding Lights" not in names and snap.more_below  # below the visible list
    assert snap.title == title and "Ready" in snap.text


@desktop
async def test_type_select_and_click(app: tuple[DesktopSession, str]) -> None:
    session, _ = app
    runner = runner_for(session)
    snap = await session.observe(120, 1500)
    typed = await act(
        runner, snap, AgentAction(op="type", element=named(snap, "Your name"), text="Bhuvan")
    )
    assert typed.ok, typed.observation
    picked = await act(
        runner, snap, AgentAction(op="select", element=named(snap, "Country"), text="Nepal")
    )
    assert picked.ok, picked.observation
    assert (await act(runner, snap, AgentAction(op="click", element=named(snap, "Save")))).ok
    after = await session.observe(120, 1500)
    assert "Saved: Bhuvan, Nepal" in after.text


@desktop
async def test_find_brings_an_off_screen_item_into_view(app: tuple[DesktopSession, str]) -> None:
    session, _ = app
    runner = runner_for(session)
    snap = await session.observe(120, 1500)
    found = await act(runner, snap, AgentAction(op="find", text="Blinding Lights"))
    assert found.ok and "Found" in found.observation
    after = await session.observe(120, 1500)
    assert "Blinding Lights" in {e.name for e in after.elements}
    missing = await act(runner, after, AgentAction(op="find", text="Bohemian Rhapsody"))
    assert missing.error == "not_found"


@desktop
async def test_a_modal_dialog_covers_its_window(app: tuple[DesktopSession, str]) -> None:
    session, _ = app
    runner = runner_for(session)
    snap = await session.observe(120, 1500)
    task = asyncio.create_task(
        act(runner, snap, AgentAction(op="click", element=named(snap, "Preferences")))
    )
    await asyncio.sleep(1.0)
    dialog = await session.observe(120, 1500)
    names = {e.name for e in dialog.elements}
    assert "Dark mode" in names and "Save" not in names  # only the dialog can be used now
    assert dialog.covered > 0 and dialog.cover == "Preferences"
    await act(runner, dialog, AgentAction(op="click", element=named(dialog, "Dark mode")))
    await act(runner, dialog, AgentAction(op="click", element=named(dialog, "OK")))
    await task
    assert "Dark mode: on" in (await session.observe(120, 1500)).text


@desktop
async def test_delete_is_confirmed_and_no_means_no(app: tuple[DesktopSession, str]) -> None:
    session, _ = app
    runner = runner_for(session, answers=[False])
    snap = await session.observe(120, 1500)
    result = await act(runner, snap, AgentAction(op="click", element=named(snap, "Delete account")))
    assert result.error == "user_declined"
    assert "Account deleted" not in (await session.observe(120, 1500)).text


Script = Callable[[str], AgentStep]


class ScriptPlanner:
    name, model = "openrouter", "scripted"

    def __init__(self, *script: Script) -> None:
        self.script = list(script)
        self.prompts: list[str] = []
        self.systems: list[str] = []

    async def decide(
        self,
        system: str,
        user: str,
        schema: Any = None,
        max_tokens: int = 0,
        image: bytes | None = None,
    ) -> Any:
        from jarvis.planner.llm import LLMReply, Usage

        self.systems.append(system)
        self.prompts.append(user)
        return LLMReply(
            self.script.pop(0)(user), "openrouter", "scripted", 1.0, Usage(100, 0, 0, 20)
        )


def el(prompt: str, name: str) -> int:
    import re

    match = re.search(rf'\[(\d+)\] [^\n"]*"{re.escape(name)}', prompt)
    assert match, f"no control named {name!r} in:\n{prompt}"
    return int(match.group(1))


@desktop
async def test_the_agent_loop_plays_a_song_in_an_app(app: tuple[DesktopSession, str]) -> None:
    session, title = app
    planner = ScriptPlanner(
        lambda p: AgentStep(thought="", status="continue",
                            actions=[AgentAction(op="find", text="Blinding Lights")]),
        lambda p: AgentStep(thought="", status="continue",
                            actions=[AgentAction(op="double_click",
                                                 element=el(p, "Blinding Lights"))]),
        lambda p: AgentStep(thought="", status="done", message="Playing Blinding Lights."),
    )  # fmt: skip
    agent = TaskAgent([planner], {"desktop": DesktopSurface(session)},
                      runner_for(session).run_action, AgentSettings())  # fmt: skip
    outcome = await agent.run(f"in {title}, play Blinding Lights", "desktop")
    assert outcome.ok, outcome.message
    assert planner.systems[0] == DESKTOP_SYSTEM
    assert 'find "Blinding Lights" → ok: Found' in planner.prompts[1]
    assert "Playing Blinding Lights" in (await session.observe(120, 1500)).text


def test_an_empty_window_is_looked_at_again() -> None:
    """A dialog still drawing shows no controls for a moment: the agent looks once more."""
    from jarvis.tiers.structured.desktop_observer import read_window

    full = Snapshot(
        url="app://notepad", title="Save as", elements=[Element(id=1, tag="button", name="Save")]
    )
    looks = [Snapshot(url="app://notepad", title="Save as"), full]

    class FakeUia:
        async def run(self, fn: Callable[..., Any], *args: Any) -> Any:
            assert fn is read_window
            return looks.pop(0), {}

    class Session(DesktopSession):
        async def current(self) -> Window | None:
            return Window(1, 1, "notepad", "Untitled - Notepad")

        async def windows(self) -> list[Window]:
            return []

    session = Session(FakeUia())  # type: ignore[arg-type]
    snap = asyncio.run(session.observe(120, 1500))
    assert [e.name for e in snap.elements] == ["Save"] and looks == []


def test_check_file_tells_where_a_file_really_is(tmp_path: Any, monkeypatch: Any) -> None:
    from jarvis.executors import desktop as desk
    from jarvis.tiers.instant import files

    desktop_dir, documents = tmp_path / "Desktop", tmp_path / "Documents"
    desktop_dir.mkdir()
    documents.mkdir()
    (documents / "jarvis.docx").write_bytes(b"x" * 2048)
    (desktop_dir / "notes.txt").write_text("hi")
    monkeypatch.setattr(files, "search_roots", lambda: [desktop_dir, documents])

    there = desk.check_file(str(desktop_dir / "notes"))  # no extension: any file with that name
    assert there.ok and "notes.txt is there" in there.observation
    missing = desk.check_file(str(desktop_dir / "jarvis.docx"))
    assert missing.error == "no_file" and str(documents / "jarvis.docx") in missing.observation


def test_check_file_is_a_safe_desktop_action() -> None:
    action, _ = to_action(
        AgentAction(op="check_file", text=r"C:\x\jarvis.docx"), snap_with(), "desktop"
    )
    assert (
        action is not None and action.kind == "check_file" and action.target == r"C:\x\jarvis.docx"
    )
    assert SafetyPolicy.from_yaml().check(action).decision == Decision.ALLOW


@desktop
async def test_fields_and_buttons_come_first_when_space_is_short(
    app: tuple[DesktopSession, str],
) -> None:
    session, _ = app
    snap = await session.observe(10, 1500)  # room for 10: the 17 visible songs must not crowd out
    names = {e.name for e in snap.elements}
    assert {"Your name", "Country", "Password", "Save", "Delete account", "Play"} <= names
    assert len(snap.elements) == 10 and snap.more_below


@desktop
async def test_attach_through_the_windows_open_dialog(
    app: tuple[DesktopSession, str], tmp_path: Any
) -> None:
    from jarvis.executors.files import FileResolver

    (tmp_path / "Resume_Bhuvan.pdf").write_bytes(b"%PDF-1.4 test")
    session, _ = app
    resolver = FileResolver([tmp_path])
    executor = DesktopExecutor(session, NoLauncher())
    runner = ActionRunner(
        SafetyPolicy.from_yaml(), executor, ScriptedConfirmer(), resolver.annotate
    )
    snap = await session.observe(120, 1500)
    op = AgentAction(op="attach", element=named(snap, "Attach file..."), text="my resume")
    result = await act(runner, snap, op)
    assert result.ok, result.observation
    assert "Attached Resume_Bhuvan.pdf" in result.observation
    assert str(tmp_path / "Resume_Bhuvan.pdf") in (await session.observe(120, 1500)).text
