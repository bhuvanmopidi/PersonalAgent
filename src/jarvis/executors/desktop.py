"""Desktop actions in any Windows app, through UI Automation (Phase 11.2). Only ever called through
ActionRunner.

The general agent works on apps exactly like on websites: it sees the window's controls as numbered
text (tiers/structured/desktop_observer.py) and acts on them by number. Kinds (when
`args["surface"] == "desktop"`): click · double_click · type_text · press_key · select_option ·
scroll · find · wait · launch_app (open an app and work in its window) · switch_window · go_back.

How a person would do it: a control's own action when it has one (a button's "press", a check box's
"toggle", a tab's "select"), otherwise a real mouse click at its centre; typing goes through the
keyboard. UI Automation is COM: every call runs on one dedicated thread (COM objects belong to the
thread that made them), so the voice loop is never blocked.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

import psutil
from rapidfuzz import fuzz

from jarvis.models import Action, ActionResult
from jarvis.telemetry.logging import get_logger
from jarvis.tiers.instant.apps import LaunchPlan, Prefer

if TYPE_CHECKING:
    from jarvis.tiers.structured.observer import Snapshot

log = get_logger(__name__)
T = TypeVar("T")

DESKTOP_KINDS = frozenset(
    {"click", "double_click", "type_text", "press_key", "select_option", "scroll", "find", "wait",
     "launch_app", "switch_window", "go_back", "check_file"}
)  # fmt: skip
WINDOW_WAIT_S = 10.0  # how long a just-launched app may take to show its window
SETTLE_QUIET_S = 0.2
SETTLE_LIMIT_S = 2.0
EMPTY_RETRY_S = 1.5  # an open window showing no controls yet gets another look
_FOLDERS = ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos")
_SKIP_PROCESSES = {"textinputhost", "searchhost", "startmenuexperiencehost", "shellexperiencehost",
                   "lockapp"}  # fmt: skip


class Launcher(Protocol):
    def resolve(self, query: str, prefer: Prefer = "auto") -> LaunchPlan | None: ...
    def launch(self, plan: LaunchPlan) -> None: ...


def ok(observation: str) -> ActionResult:
    return ActionResult(ok=True, observation=observation)


def fail(error: str, observation: str) -> ActionResult:
    return ActionResult(ok=False, error=error, observation=observation)


class UiaThread:
    """One thread for every UI Automation call (COM apartment rules)."""

    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="uia",
                                        initializer=self._init)  # fmt: skip

    @staticmethod
    def _init() -> None:
        import uiautomation as auto

        auto.InitializeUIAutomationInCurrentThread()
        auto.SetGlobalSearchTimeout(1)

    async def run(self, fn: Callable[..., T], *args: Any) -> T:
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


@dataclass(frozen=True)
class Window:
    handle: int
    pid: int
    exe: str  # "spotify" (no .exe), lower case
    title: str


def own_pids() -> set[int]:
    """Jarvis and everything it runs inside (the terminal, VS Code): never a target window."""
    me = psutil.Process()
    return {me.pid} | {p.pid for p in me.parents()}


def exe_of(pid: int) -> str:
    try:
        return psutil.Process(pid).name().lower().removesuffix(".exe")
    except psutil.Error:
        return ""


_SHELL_CLASSES = {
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "Progman",
    "WorkerW",
}  # taskbar, desktop
_WS_EX_TOOLWINDOW, _WS_EX_NOACTIVATE = 0x80, 0x08000000


def _is_app_window(handle: int) -> bool:
    """Not a tool window or an overlay that can't be activated (e.g. a GPU overlay)."""
    import ctypes

    style = int(ctypes.windll.user32.GetWindowLongW(handle, -20))
    return not style & (_WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE)


def _hosted_exe(frame: Any) -> str:
    for child in frame.GetChildren():
        if child.ProcessId != frame.ProcessId:
            return exe_of(child.ProcessId)
    return ""


def match_score(query: str, window: Window) -> float:
    """How well an app name ("spotify", "file explorer") fits a window (title or program)."""
    q = query.lower().strip()
    if not q:
        return 0.0
    title, exe = window.title.lower(), window.exe
    whole_word = len(q) >= 4 and re.search(rf"(?<!\w){re.escape(q)}(?!\w)", title)
    if q == exe or q in [s.strip() for s in title.split(" - ")] or whole_word:
        return 100.0
    return max(float(fuzz.partial_ratio(q, title)) if len(q) >= 4 else 0.0,
               float(fuzz.ratio(q, exe)))  # fmt: skip


class DesktopSession:
    """Which window the agent works in, and the controls of its last look (by number)."""

    def __init__(self, uia: UiaThread | None = None) -> None:
        self.uia = uia or UiaThread()
        self.window: Window | None = None
        self.controls: dict[int, Any] = {}  # [number] → UIA control, from the last observe()
        self._skip = own_pids()
        self._folders: dict[str, str] | None = None

    # ---- windows (all run on the UIA thread) ---------------------------------------------------

    def _windows(self) -> list[Window]:
        """Visible top-level app windows, front-most first (Jarvis's own terminal excluded)."""
        import uiautomation as auto

        found = []
        for w in auto.GetRootControl().GetChildren():
            try:
                if not w.Name or w.ProcessId in self._skip:
                    continue
                if w.ClassName in _SHELL_CLASSES or not _is_app_window(w.NativeWindowHandle):
                    continue
                # a minimized app (no size on screen) still counts: it is restored when used
                minimized = bool(auto.IsIconic(w.NativeWindowHandle))
                rect = w.BoundingRectangle
                if not minimized and (w.IsOffscreen or rect.width() < 50 or rect.height() < 50):
                    continue
                exe = exe_of(w.ProcessId)
                if exe == "applicationframehost":  # Store apps (Calculator, Settings): the real
                    exe = _hosted_exe(w)  # app is a child window of another process
                    if not exe and not minimized:
                        continue  # an empty frame left behind by a closed app — not a window
                    exe = exe or "applicationframehost"
                if exe in _SKIP_PROCESSES:
                    continue
                found.append(Window(w.NativeWindowHandle, w.ProcessId, exe, w.Name))
            except Exception:  # a window closed while we looked
                continue
        return found

    def _control(self, window: Window) -> Any:
        import uiautomation as auto

        return auto.ControlFromHandle(window.handle)

    def _alive(self, window: Window) -> bool:
        import ctypes

        import uiautomation as auto

        is_window = ctypes.windll.user32.IsWindow(window.handle)
        return bool(is_window) and bool(auto.IsWindowVisible(window.handle))

    def _activate(self, window: Window) -> None:
        import uiautomation as auto

        ctrl = self._control(window)
        if ctrl is None:
            return
        if auto.IsIconic(window.handle):
            auto.ShowWindow(window.handle, auto.SW.Restore)
        try:
            ctrl.SetActive()
        except Exception:
            ctrl.SetFocus()
        for _ in range(40):  # a restored window has no size for a moment: wait until it shows
            rect = ctrl.BoundingRectangle
            if rect.width() > 0 and rect.height() > 0:
                return
            time.sleep(0.05)

    def _best(self, query: str, windows: list[Window]) -> Window | None:
        scored = sorted(((match_score(query, w), i, w) for i, w in enumerate(windows)),
                        key=lambda s: (-s[0], s[1]))  # fmt: skip
        return scored[0][2] if scored and scored[0][0] >= 80 else None

    # ---- async API ------------------------------------------------------------------------------

    async def windows(self) -> list[Window]:
        return await self.uia.run(self._windows)

    async def current(self) -> Window | None:
        """The working window — None when it closed (the agent must open or switch to one)."""
        if self.window is not None and not await self.uia.run(self._alive, self.window):
            self.window = None
        return self.window

    async def use(self, window: Window) -> None:
        self.window = window
        await self.uia.run(self._activate, window)

    async def switch(self, query: str) -> Window | None:
        best = self._best(query, await self.windows())
        if best is not None:
            await self.use(best)
        return best

    async def wait_for_app(
        self, query: str, before: set[int], plan: LaunchPlan | None
    ) -> Window | None:
        """After launching: a new window, or (already running app) the window that matches."""
        names = [query] + ([plan.display] if plan else [])
        deadline = time.monotonic() + WINDOW_WAIT_S
        while time.monotonic() < deadline:
            windows = await self.windows()
            new = [w for w in windows if w.handle not in before]
            for name in names:
                pick = self._best(name, new) or self._best(name, windows)
                if pick is not None:
                    await self.use(pick)
                    return pick
            if len(new) == 1 and time.monotonic() > deadline - WINDOW_WAIT_S / 2:
                await self.use(new[0])  # one new window, whatever its title says
                return new[0]
            await asyncio.sleep(0.25)
        return None

    async def observe(self, max_elements: int, max_text: int) -> Snapshot:
        from jarvis.tiers.structured.desktop_observer import read_window

        window = await self.current()
        others = [
            w.title for w in await self.windows() if window is None or w.handle != window.handle
        ]
        deadline = time.monotonic() + EMPTY_RETRY_S
        while True:
            try:
                snap, controls = await self.uia.run(
                    read_window, window, others, max_elements, max_text
                )
            except Exception:  # the window died while we looked (COM error): it's gone
                log.info("window %s went away while being read", window)
                self.window = window = None
                snap, controls = await self.uia.run(
                    read_window, None, others, max_elements, max_text
                )
            # an open window with nothing in it is still drawing (a dialog opening): look again
            if window is None or snap.elements or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.25)
        self.controls = controls
        return snap.model_copy(update={"folders": self.folders()})

    def folders(self) -> dict[str, str]:
        """The user's real folders (Desktop may be in OneDrive) — so a Save dialog gets a full path
        instead of a guess."""
        if self._folders is None:
            from jarvis.tiers.instant.files import known_folder

            found = {name: known_folder(name.lower()) for name in _FOLDERS}
            self._folders = {name: str(p) for name, p in found.items() if p is not None}
        return self._folders

    async def size(self) -> int:
        """A cheap "did anything appear or go away?" number for the working window."""
        from jarvis.tiers.structured.desktop_observer import count_controls

        window = await self.current()
        if window is None:
            return -1
        try:
            return await self.uia.run(count_controls, window)
        except Exception:  # going away
            return -1

    async def settle(self) -> None:
        """Wait until the window stops changing (0.2 s quiet, at most 2 s)."""
        start, last, since = time.monotonic(), await self.size(), time.monotonic()
        while time.monotonic() - start < SETTLE_LIMIT_S:
            await asyncio.sleep(0.05)  # a look at the tree itself takes ~0.1 s
            now = await self.size()
            if now != last:
                last, since = now, time.monotonic()
            elif time.monotonic() - since >= SETTLE_QUIET_S:
                return

    async def still_there(self, element_id: int) -> bool:
        ctrl = self.controls.get(element_id)
        if ctrl is None:
            return False

        def alive() -> bool:
            try:
                rect = ctrl.BoundingRectangle
                return bool(rect.width() > 0 and rect.height() > 0)
            except Exception:
                return False

        return await self.uia.run(alive)

    def location(self) -> str:
        return f"app://{self.window.exe}" if self.window else "app://none"

    async def needs_admin(self) -> bool:
        """Windows is asking for administrator permission (the UAC prompt runs as consent.exe)."""
        return await asyncio.to_thread(
            lambda: any(p.info["name"] == "consent.exe" for p in psutil.process_iter(["name"]))
        )

    def close(self) -> None:
        self.uia.close()


# ---- keys ----------------------------------------------------------------------------------------

_KEY_NAMES = {
    "enter": "enter", "return": "enter", "esc": "esc", "escape": "esc", "tab": "tab",
    "space": "space", "backspace": "backspace", "delete": "delete", "del": "delete",
    "up": "up", "arrowup": "up", "down": "down", "arrowdown": "down", "left": "left",
    "arrowleft": "left", "right": "right", "arrowright": "right", "home": "home", "end": "end",
    "pageup": "page_up", "pagedown": "page_down", "insert": "insert", "menu": "menu",
    **{f"f{i}": f"f{i}" for i in range(1, 13)},
}  # fmt: skip
_MODIFIERS = {"ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift", "win": "cmd",
              "windows": "cmd", "cmd": "cmd"}  # fmt: skip


def parse_keys(combo: str) -> tuple[list[str], str] | None:
    """'Ctrl+Shift+N' → (['ctrl', 'shift'], 'n'); None if it isn't a key we know."""
    parts = [p.strip().lower() for p in re.split(r"\s*\+\s*", combo.strip()) if p.strip()]
    if not parts:
        return None
    *mods, key = parts
    if any(m not in _MODIFIERS for m in mods):
        return None
    if key not in _KEY_NAMES and len(key) != 1:
        return None
    return [_MODIFIERS[m] for m in mods], key


class Keyboard(Protocol):
    def press(self, combo: str) -> bool: ...
    def type(self, text: str) -> None: ...


class PynputKeyboard:
    """Real key presses (injected — the Esc kill switch ignores keys the agent sends itself)."""

    def __init__(self) -> None:
        from pynput.keyboard import Controller

        self.kb = Controller()

    def press(self, combo: str) -> bool:
        from pynput.keyboard import Key

        parsed = parse_keys(combo)
        if parsed is None:
            return False
        mods, key = parsed
        target: Any = getattr(Key, _KEY_NAMES[key]) if key in _KEY_NAMES else key
        held = [getattr(Key, m) for m in mods]
        for m in held:
            self.kb.press(m)
        try:
            self.kb.press(target)
            self.kb.release(target)
        finally:
            for m in reversed(held):
                self.kb.release(m)
        return True

    def type(self, text: str) -> None:
        for ch in text:
            self.kb.type(ch)
            time.sleep(0.008)


# ---- the executor --------------------------------------------------------------------------------

_INVOKE_TYPES = {"ButtonControl", "MenuItemControl", "HyperlinkControl", "SplitButtonControl"}
_TOGGLE_TYPES = {"CheckBoxControl", "ButtonControl"}
_SELECT_TYPES = {"TabItemControl", "RadioButtonControl"}
_SINGLE_LINE = {"EditControl", "ComboBoxControl"}


class DesktopExecutor:
    def __init__(self, session: DesktopSession, launcher: Launcher,
                 keyboard: Keyboard | None = None) -> None:  # fmt: skip
        self.session = session
        self.launcher = launcher
        self._keyboard = keyboard

    @property
    def keyboard(self) -> Keyboard:
        if self._keyboard is None:
            self._keyboard = PynputKeyboard()
        return self._keyboard

    async def execute(self, action: Action) -> ActionResult:
        kind, a = action.kind, action.args
        what = f'"{action.target}"' if action.target else f"[{a.get('element')}]"
        if kind == "launch_app":
            return await self._open_app(action.target)
        if kind == "check_file":
            return await asyncio.to_thread(check_file, action.target)
        if kind == "switch_window":
            window = await self.session.switch(action.target)
            if window is None:
                return fail("no_window", f'No open window matches "{action.target}"')
            return ok(f'Switched to "{window.title}"')
        window = await self.session.current()
        if window is None:
            return fail("no_window", "No app window to work in — open or switch to one first")
        await self.session.uia.run(self.session._activate, window)
        # the agent waits for the window to settle before it looks again — no wait here
        no_element = a.get("element") is None
        if (
            kind in ("scroll", "find", "wait", "go_back")
            or no_element
            and kind in ("press_key", "type_text")
        ):
            return await self._window_op(action)
        ctrl = self.session.controls.get(int(a["element"])) if not no_element else None
        if ctrl is None:
            return fail("bad_element", f"{what} isn't on the screen any more — look again")
        return await self.session.uia.run(self._element_op, kind, ctrl, a, what)

    async def _open_app(self, name: str) -> ActionResult:
        running = await self.session.switch(name)
        if running is not None:
            return ok(f'"{running.title}" is open')
        plan = await asyncio.to_thread(self.launcher.resolve, name, "app")
        if plan is None or plan.kind == "url":
            return fail("unknown_app", f'I can\'t find an app called "{name}" on this PC')
        before = {w.handle for w in await self.session.windows()}
        await asyncio.to_thread(self.launcher.launch, plan)
        window = await self.session.wait_for_app(name, before, plan)
        if window is None:
            return fail("no_window", f"{plan.display} started, but its window didn't appear")
        await self.session.settle()
        return ok(f'Opened "{window.title}"')

    async def _window_op(self, action: Action) -> ActionResult:
        kind, a = action.kind, action.args
        if kind == "wait":
            await asyncio.sleep(min(float(a.get("seconds", 1.0)), 5.0))
            await self.session.settle()
            return ok("Waited")
        if kind == "go_back":
            await asyncio.to_thread(self.keyboard.press, "Alt+Left")
            return ok("Went back")
        if kind == "press_key":
            key = str(a.get("key") or "Enter")
            pressed = await asyncio.to_thread(self.keyboard.press, key)
            if not pressed:
                return fail("bad_key", f"I don't know the key '{key}'")
            return ok(f"Pressed {key}")
        if kind == "type_text":  # no box named: type on the keyboard, where the cursor is
            if await self.session.uia.run(_focus_is_password):
                return fail("password_field", "The cursor is in a password box — you type it")
            await asyncio.to_thread(self.keyboard.type, str(a.get("text", "")))
            return ok("Typed it")
        if kind == "scroll":
            up = str(a.get("direction", "down")).lower() == "up"
            how = await self.session.uia.run(_scroll_window, self.session.window, up)
            return ok(
                f"Scrolled {'up' if up else 'down'}" + (" (mouse wheel)" if how == "wheel" else "")
            )
        # find
        words = str(a.get("text", "")).strip()
        if not words:
            return fail("bad_find", "find needs the words to look for")
        found = await self.session.uia.run(_find_in_window, self.session.window, words)
        if not found:
            return fail("not_found", f'"{words}" is not anywhere in this window')
        count, name = found
        more = f" ({count} matches)" if count > 1 else ""
        return ok(f'Found "{words}"{more} — scrolled to it: "{name}"')

    def _element_op(self, kind: str, ctrl: Any, a: dict[str, Any], what: str) -> ActionResult:
        """Runs on the UIA thread."""
        ctype = ctrl.ControlTypeName
        try:
            if kind == "click":
                return ok(f"Clicked {what}" + _click(ctrl, ctype))
            if kind == "double_click":
                _scroll_into_view(ctrl)
                ctrl.DoubleClick(simulateMove=False)
                return ok(f"Double-clicked {what}")
            if kind == "press_key":
                ctrl.SetFocus()
                key = str(a.get("key") or "Enter")
                return ok(f"Pressed {key}") if self.keyboard.press(key) else fail(
                    "bad_key", f"I don't know the key '{key}'")  # fmt: skip
            if kind == "type_text":
                return self._type(ctrl, ctype, str(a.get("text", "")), what)
            if kind == "select_option":
                return _select(ctrl, str(a.get("text", "")), what, self.keyboard)
        except Exception as exc:  # COM errors: the control went away, isn't enabled, …
            return fail("element_not_ready", f"Couldn't {kind.replace('_', ' ')} {what}: {exc}")
        return fail("unsupported", f"Unknown desktop action '{kind}'")

    def _type(self, ctrl: Any, ctype: str, text: str, what: str) -> ActionResult:
        if getattr(ctrl, "IsPassword", False):  # the policy hands these over; never type here
            return fail("password_field", "That's a password box — you type it")
        _scroll_into_view(ctrl)
        ctrl.SetFocus()
        if not ctrl.HasKeyboardFocus:
            ctrl.Click(simulateMove=False)
        if ctype in _SINGLE_LINE:
            self.keyboard.press("Ctrl+A")  # replace a one-line field's content, like a person
        self.keyboard.type(text)  # documents: typed where the cursor is, nothing is replaced
        return ok(f"Typed into {what}")


def check_file(path_text: str) -> ActionResult:
    """Is the file really there? (No extension given: any file with that name.) If not, where are
    files with that name? So "saved on your Desktop" is a fact, not a belief. Read-only."""
    from datetime import datetime
    from pathlib import Path

    from jarvis.tiers.instant.files import find_files, search_roots

    path = Path(path_text.strip().strip('"'))
    candidates = [path] if path.suffix else sorted(path.parent.glob(path.name + ".*"))
    for found in candidates:
        if found.is_file():
            stat = found.stat()
            when = datetime.fromtimestamp(stat.st_mtime).strftime("%H:%M on %d %b")
            return ok(f"{found} is there ({max(1, stat.st_size // 1024)} KB, saved {when})")
    elsewhere = [p for p in find_files(path.stem, search_roots(), limit=5) if p.stem == path.stem]
    hint = f"; a file with that name is at: {', '.join(map(str, elsewhere))}" if elsewhere else ""
    return fail("no_file", f"{path} is not there{hint}")


def _focus_is_password() -> bool:
    import uiautomation as auto

    try:
        focused = auto.GetFocusedControl()
        return bool(focused is not None and focused.IsPassword)
    except Exception:
        return False


def _on_screen(ctrl: Any) -> bool:
    try:
        rect = ctrl.BoundingRectangle
        return not ctrl.IsOffscreen and rect.width() > 0 and rect.height() > 0
    except Exception:
        return False


def _scroll_into_view(ctrl: Any) -> None:
    """Bring an off-screen control into view: its own "scroll into view" if it has one, else
    scroll its list / pane step by step (from the top) until it shows."""
    import uiautomation as auto

    if _on_screen(ctrl):
        return
    pattern = ctrl.GetPattern(auto.PatternId.ScrollItemPattern)
    if pattern is not None:
        try:
            pattern.ScrollIntoView()
            if _on_screen(ctrl):
                return
        except Exception:
            pass
    pane, parent = None, ctrl.GetParentControl()
    for _ in range(8):  # the nearest container that scrolls
        if parent is None:
            return
        scroll = parent.GetPattern(auto.PatternId.ScrollPattern)
        if scroll is not None and scroll.VerticallyScrollable:
            pane = scroll
            break
        parent = parent.GetParentControl()
    if pane is None:
        return
    with contextlib.suppress(Exception):  # start from the top of the list
        pane.SetScrollPercent(auto.ScrollPattern.NoScrollValue, 0)
    for _ in range(40):
        if _on_screen(ctrl) or pane.VerticalScrollPercent >= 100:
            return
        pane.Scroll(auto.ScrollAmount.NoAmount, auto.ScrollAmount.LargeIncrement)
        time.sleep(0.05)


def _click(ctrl: Any, ctype: str) -> str:
    """How a person clicks: a real mouse click on what's on screen. A check box / tab / drop-down
    uses its own toggle / select / open. A button's own "press" (Invoke) is only the fallback for
    one that can't be clicked: Invoke waits until a dialog it opens is closed again."""
    import uiautomation as auto

    _scroll_into_view(ctrl)
    if ctype in _INVOKE_TYPES:
        if _on_screen(ctrl):
            ctrl.Click(simulateMove=False)
            return ""
        if inv := ctrl.GetPattern(auto.PatternId.InvokePattern):
            inv.Invoke()
            return ""
    if ctype in _TOGGLE_TYPES and (tog := ctrl.GetPattern(auto.PatternId.TogglePattern)):
        tog.Toggle()
        return ""
    if ctype in _SELECT_TYPES and (sel := ctrl.GetPattern(auto.PatternId.SelectionItemPattern)):
        sel.Select()
        return ""
    if ctype == "ComboBoxControl" and (
        exp := ctrl.GetPattern(auto.PatternId.ExpandCollapsePattern)
    ):
        exp.Expand()
        return " (opened its list)"
    ctrl.Click(simulateMove=False)
    return ""


def _select(ctrl: Any, text: str, what: str, keyboard: Keyboard) -> ActionResult:
    import uiautomation as auto

    want = text.lower().strip()
    exp = ctrl.GetPattern(auto.PatternId.ExpandCollapsePattern)
    if exp is not None:
        exp.Expand()
        time.sleep(0.2)
    for item, _ in auto.WalkControl(ctrl, maxDepth=4):
        if item.ControlTypeName == "ListItemControl" and item.Name.lower().strip() == want:
            sel = item.GetPattern(auto.PatternId.SelectionItemPattern)
            if sel is not None:
                sel.Select()
            else:
                item.Click(simulateMove=False)
            if exp is not None:
                with contextlib.suppress(Exception):  # many lists close by themselves
                    exp.Collapse()
            return ok(f'Selected "{item.Name}" in {what}')
    # no list to pick from: type it, like a person in a searchable drop-down
    ctrl.SetFocus()
    keyboard.type(text)
    keyboard.press("Enter")
    return ok(f'Typed "{text}" into {what} and pressed Enter')


def window_root(window: Window) -> Any:
    """The window's UI Automation control; raises if the window is gone."""
    import uiautomation as auto

    root = auto.ControlFromHandle(window.handle)
    if root is None:
        raise LookupError(f'the window "{window.title}" is gone')
    return root


def _scroll_window(window: Window | None, up: bool) -> str:
    """Scroll the biggest scrollable area of the window; mouse wheel if none says it scrolls."""
    import uiautomation as auto

    if window is None:
        return "none"
    root = window_root(window)
    best, area = None, 0
    for ctrl, _ in auto.WalkControl(root, maxDepth=12):
        pattern = ctrl.GetPattern(auto.PatternId.ScrollPattern)
        if pattern is not None and pattern.VerticallyScrollable:
            rect = ctrl.BoundingRectangle
            if rect.width() * rect.height() > area:
                best, area = pattern, rect.width() * rect.height()
    if best is not None:
        amount = auto.ScrollAmount.LargeDecrement if up else auto.ScrollAmount.LargeIncrement
        best.Scroll(auto.ScrollAmount.NoAmount, amount)
        return "pattern"
    rect = root.BoundingRectangle
    auto.MoveTo((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2, moveSpeed=0)
    if up:
        auto.WheelUp(wheelTimes=5)
    else:
        auto.WheelDown(wheelTimes=5)
    return "wheel"


def _find_in_window(window: Window | None, words: str) -> tuple[int, str] | None:
    """Ctrl+F for apps: search every control's name (off-screen ones too), bring the first in."""
    import uiautomation as auto

    if window is None:
        return None
    want = words.lower()
    root = window_root(window)
    hits = []
    for ctrl, _ in auto.WalkControl(root, maxDepth=30):
        try:
            if want in (ctrl.Name or "").lower():
                hits.append(ctrl)
        except Exception:
            continue
        if len(hits) >= 50:
            break
    if not hits:
        return None
    first = hits[0]
    _scroll_into_view(first)
    return len(hits), (first.Name or "")[:80]
