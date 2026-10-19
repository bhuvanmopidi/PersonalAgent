"""See, for Windows apps: a window's UI Automation tree → the same numbered Snapshot the browser
agent reads ([n] button "Play", [n] edit "Search" value="…"). Runs on the UIA thread
(executors/desktop.py).

Listed: the controls a person can use (buttons, fields, lists, menus, tabs, links, check boxes …)
that are on screen. Off-screen ones (the rest of a long list) are counted, not listed — `find`
or `scroll` reaches them. Plain text in the window (labels, a calculator's display, song titles)
becomes the text excerpt. A modal dialog (Save As, Options…) blocks its window, so only the dialog
is listed and the note says what covers the rest. Password boxes are flagged; their content is
never read.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from jarvis.tiers.structured.observer import Element, Snapshot

if TYPE_CHECKING:
    from jarvis.executors.desktop import Window

MAX_VISIT = 1500  # nodes per look — big app trees (Electron) are cut, not waited for
_TAGS = {
    "ButtonControl": "button", "EditControl": "edit", "ComboBoxControl": "combo box",
    "CheckBoxControl": "check box", "RadioButtonControl": "radio button",
    "ListItemControl": "list item", "MenuItemControl": "menu item", "TabItemControl": "tab",
    "HyperlinkControl": "link", "TreeItemControl": "tree item", "DataItemControl": "row",
    "SplitButtonControl": "split button", "SliderControl": "slider", "SpinnerControl": "spinner",
    "DocumentControl": "document", "HeaderItemControl": "column header",
}  # fmt: skip
_SKIP_SUBTREE = {
    "TitleBarControl",
    "ScrollBarControl",
}  # window chrome: close/minimize, scroll bars
_PRIVATE_USE = re.compile(r"[-]")  # icon-font glyphs (, …) mean nothing as text


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", _PRIVATE_USE.sub("", text or "")).strip()


def _cut(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _dialogs(window: Window, root: Any) -> list[Any]:
    """The app's dialogs and pop-up menus, front-most first: its other top-level windows, and the
    windows UI Automation shows INSIDE the main one (that's where owned dialogs appear)."""
    import uiautomation as auto

    def shown(w: Any) -> bool:
        rect = w.BoundingRectangle
        return not w.IsOffscreen and rect.width() > 20 and rect.height() > 20

    found = []
    # Store apps all live in one host process (ApplicationFrameHost): its other windows are other
    # apps (Calculator next to Settings), not dialogs
    shared_host = window.exe == "applicationframehost" or _hosted(window, root) is not None
    for w in auto.GetRootControl().GetChildren():
        try:
            if shared_host:
                break
            if w.ProcessId == window.pid and w.NativeWindowHandle != window.handle and shown(w):
                found.append(w)
        except Exception:
            continue
    for w in root.GetChildren():
        try:
            # a real dialog behaves like a window (can close, may be modal); a Store app's inner
            # title bar / content windows don't, and aren't dialogs
            is_window = w.GetPattern(auto.PatternId.WindowPattern) is not None
            if w.ControlTypeName == "WindowControl" and is_window and shown(w):
                found.append(w)
        except Exception:
            continue
    return found


def _hosted(window: Window, root: Any) -> str | None:
    """A Store app's real program (Settings → systemsettings), from the content window its frame
    hosts — known once the app is running in front. None for ordinary windows."""
    from jarvis.executors.desktop import exe_of

    if exe_of(window.pid) != "applicationframehost":
        return None
    for child in root.GetChildren():
        try:
            if child.ProcessId != window.pid:
                return exe_of(child.ProcessId) or None
        except Exception:
            continue
    return None


def _is_modal(dialog: Any) -> bool:
    import uiautomation as auto

    pattern = dialog.GetPattern(auto.PatternId.WindowPattern)
    try:
        return bool(pattern and pattern.IsModal)
    except Exception:
        return False


def _element(ctrl: Any, ctype: str, eid: int) -> Element:
    import uiautomation as auto

    password = bool(getattr(ctrl, "IsPassword", False))
    value = ""
    if not password and ctype in ("EditControl", "ComboBoxControl", "DocumentControl"):
        pattern = ctrl.GetPattern(auto.PatternId.ValuePattern)
        if pattern is not None:
            try:
                value = _cut(clean(pattern.Value), 60)
            except Exception:
                value = ""
    if not value and ctype == "ComboBoxControl":  # many drop-downs only say what's selected
        selection = ctrl.GetPattern(auto.PatternId.SelectionPattern)
        try:
            chosen = selection.GetSelection() if selection is not None else []
            value = _cut(clean(chosen[0].Name), 60) if chosen else ""
        except Exception:
            value = ""
    checked: bool | None = None
    toggle = ctrl.GetPattern(auto.PatternId.TogglePattern)
    if toggle is not None:
        checked = toggle.ToggleState == auto.ToggleState.On
    elif ctype in ("ListItemControl", "TabItemControl", "RadioButtonControl", "TreeItemControl"):
        sel = ctrl.GetPattern(auto.PatternId.SelectionItemPattern)
        if sel is not None:
            checked = bool(sel.IsSelected)
    return Element(
        id=eid,
        tag=_TAGS[ctype],
        name=_cut(clean(ctrl.Name), 80),
        value=value,
        checked=checked,
        disabled=not ctrl.IsEnabled,
        password=password,
        focused=bool(ctrl.HasKeyboardFocus),
    )


Bounds = tuple[int, int, int, int]  # left, top, right, bottom


def bounds_of(ctrl: Any) -> Bounds:
    r = ctrl.BoundingRectangle
    return r.left, r.top, r.right, r.bottom


def _visible(child: Any, bounds: Bounds) -> bool:
    """On screen AND inside the window. Modern apps keep scrolled-away items in the tree and
    call them "on screen" — their position, outside the window, gives them away."""
    if child.IsOffscreen:
        return False
    r = child.BoundingRectangle
    if r.width() <= 0 or r.height() <= 0:
        return False
    left, top, right, bottom = bounds
    return bool(r.right > left and r.left < right and r.bottom > top and r.top < bottom)


# when there are more controls than fit, these come last: long lists of items (folders, files,
# rows) — the fields and buttons that do things (a dialog's File name box, Save) must not be cut
_BULK = {"ListItemControl", "TreeItemControl", "DataItemControl", "HeaderItemControl"}


class _Reader:
    def __init__(self, max_elements: int, bounds: Bounds, skip: set[int] | None = None) -> None:
        self.max_elements = max_elements
        self.bounds = bounds
        self.skip = skip or set()  # dialog windows inside the tree: read separately
        self.found: list[tuple[str, Any]] = []  # (control type, control), in screen order
        self.elements: list[Element] = []
        self.controls: dict[int, Any] = {}
        self.texts: list[str] = []
        self.hidden = 0  # off-screen controls (more of a list, a scrolled-away area)
        self.truncated = False
        self.visited = 0

    def finish(self) -> None:
        """Number what fits: every field / button first, then list items in order; screen order
        is kept in the numbering."""
        keep = [i for i, (ctype, _) in enumerate(self.found) if ctype not in _BULK]
        room = self.max_elements - len(keep)
        keep += [i for i, (ctype, _) in enumerate(self.found) if ctype in _BULK][: max(0, room)]
        keep = sorted(keep[: self.max_elements])
        if len(keep) < len(self.found):
            self.truncated = True
        for i in keep:
            ctype, ctrl = self.found[i]
            try:
                eid = len(self.elements) + 1
                self.elements.append(_element(ctrl, ctype, eid))
                self.controls[eid] = ctrl
            except Exception:  # gone meanwhile
                continue

    def walk(self, ctrl: Any, inside_listed: bool = False) -> None:
        try:
            children = ctrl.GetChildren()
        except Exception:
            return
        for child in children:
            self.visited += 1
            if self.visited > MAX_VISIT:
                self.truncated = True
                return
            try:
                ctype = child.ControlTypeName
                if ctype in _SKIP_SUBTREE:
                    continue
                if ctype == "WindowControl" and child.NativeWindowHandle in self.skip:
                    continue
                if not _visible(child, self.bounds):
                    if ctype in _TAGS:
                        self.hidden += 1
                    continue  # not on screen: neither it nor what it contains can be used now
                if ctype in _TAGS:
                    if len(self.found) < self.max_elements * 4:
                        self.found.append((ctype, child))
                    else:
                        self.truncated = True
                    self.walk(child, inside_listed=True)
                    continue
                if ctype == "TextControl" and not inside_listed:
                    name = clean(child.Name)
                    if name:
                        self.texts.append(name)
                self.walk(child, inside_listed)
            except Exception:  # the control changed while we looked
                continue


def read_window(
    window: Window | None, others: list[str], max_elements: int, max_text: int
) -> tuple[Snapshot, dict[int, Any]]:
    """One look at the working window. Returns the snapshot and [number] → control."""

    other_windows = [_cut(t, 50) for t in others[:15]]
    if window is None:
        snap = Snapshot(url="app://none", title="(no app window yet)", windows=other_windows)
        return snap, {}

    from jarvis.executors.desktop import window_root

    root = window_root(window)
    dialogs = _dialogs(window, root)
    skip = {d.NativeWindowHandle for d in dialogs}
    modal = [d for d in dialogs if _is_modal(d)]
    reader = _Reader(max_elements, bounds_of(root), skip)
    for dialog in dialogs:  # front-most first: what the user would deal with first
        reader.bounds = bounds_of(dialog)
        reader.walk(dialog)
    reader.bounds = bounds_of(root)
    covered, cover, share = 0, "", 0.0
    if modal:
        behind = _Reader(max_elements, bounds_of(root), skip)
        behind.walk(root)
        covered = len(behind.found)
        cover = _cut(clean(modal[0].Name), 70) or "a dialog"
        d, w = modal[0].BoundingRectangle, root.BoundingRectangle
        share = round(min(1.0, (d.width() * d.height()) / max(1, w.width() * w.height())), 2)
    else:
        reader.walk(root)
    reader.finish()

    text = _cut(" · ".join(dict.fromkeys(reader.texts)), max_text)
    exe = _hosted(window, root) or window.exe  # Store apps: the real program, for the safety rules
    snap = Snapshot(
        url=f"app://{exe}",
        title=clean(root.Name) or window.title,
        headings=[_cut(clean(d.Name), 60) for d in dialogs if clean(d.Name)],
        text=text,
        elements=reader.elements,
        more_below=reader.hidden > 0 or reader.truncated,
        covered=covered,
        cover=cover,
        cover_share=share,
        windows=other_windows,
    )
    return snap, reader.controls


COUNT_BUDGET = 400  # nodes: enough to notice a list or dialog appearing, cheap in big apps


def count_controls(window: Window) -> int:
    """How much is on screen (visible controls + dialogs): changes when something appears or goes
    away. Only what's inside the window is walked, so it stays fast in big apps."""
    from jarvis.executors.desktop import window_root

    root = window_root(window)
    bounds, total, stack = bounds_of(root), 0, [root]
    while stack and total < COUNT_BUDGET:
        try:
            children = stack.pop().GetChildren()
        except Exception:
            continue
        for child in children:
            try:
                if _visible(child, bounds):
                    total += 1
                    stack.append(child)
            except Exception:
                continue
    return total + 10_000 * len(_dialogs(window, root))
