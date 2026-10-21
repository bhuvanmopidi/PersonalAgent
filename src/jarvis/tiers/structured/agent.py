"""The general agent (Tier 3): does a task on any website or in any Windows app — See → Think →
Act → Check.

  observe the surface (a browser tab or an app window: numbered elements + text)  →  the planner
  (Claude Haiku via OpenRouter) picks 1–5 actions  →  each action goes through ActionRunner (safety
  policy: confirm / hand off / refuse, never a bypass)  →  look again  →  until done / ask / fail /
  out of steps or time.

No site- or app-specific code: the same loop works on YouTube, Amazon, Gmail, Spotify, Settings,
File Explorer or a college portal.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis.config import AgentSettings
from jarvis.executors.browser_session import BrowserUnavailable
from jarvis.models import Action, ActionResult
from jarvis.paths import data_dir
from jarvis.planner.llm import LLMProvider, LLMUnavailable, Usage
from jarvis.safety.redact import redact
from jarvis.telemetry.logging import get_logger
from jarvis.tiers.structured.observer import Element, Snapshot, what_changed
from jarvis.tiers.structured.prompts import DESKTOP_SYSTEM, SYSTEM, user_prompt
from jarvis.tiers.structured.schema import MAX_ACTIONS, AgentAction, AgentStep
from jarvis.tiers.structured.surface import BrowserSurface, Surface, SurfaceName
from jarvis.tiers.vision.eyes import Eyes, Look

if TYPE_CHECKING:
    from jarvis.executors.browser import BrowserExecutor

log = get_logger(__name__)

RunAction = Callable[[Action], Coroutine[Any, Any, ActionResult]]

# the user said no / cancelled, the policy refused, or the window is gone: stop, don't work around
_STOP = {"user_declined", "user_cancelled", "refused", "browser_closed", "browser_unavailable",
         "admin"}  # fmt: skip
STOP_ERRORS = _STOP  # the user said no / cancelled, or the policy refused: never work around it
_KIND = {
    "click": "click", "double_click": "double_click", "type": "type_text", "press": "press_key",
    "select": "select_option", "scroll": "scroll", "find": "find", "navigate": "navigate",
    "back": "go_back", "wait": "wait", "open_app": "launch_app", "switch": "switch_window",
    "check_file": "check_file", "attach": "attach_file", "wait_download": "wait_download",
    "open_file": "open_file", "new_tab": "tab_open", "switch_tab": "tab_switch",
    "close_tab": "tab_close", "private_window": "private_window",
}  # fmt: skip
_TAB_OPS = {"new_tab", "switch_tab", "close_tab", "private_window"}  # browser only
_WEB = ("http://", "https://", "file://")
_LOOK = {"look"}  # both surfaces: see the screen (vision) on the next round
MAX_LOOKS_PER_PAGE = 3
_FILE_OPS = {"attach", "wait_download", "open_file"}  # both surfaces: files in and out
# what each surface can do (the planner is told; anything else is rejected before it runs)
OPS: dict[SurfaceName, frozenset[str]] = {
    "browser": frozenset({"click", "double_click", "type", "press", "select", "scroll", "find",
                          "navigate", "back", "wait", *_FILE_OPS, *_TAB_OPS, *_LOOK}),
    "desktop": frozenset({"click", "double_click", "type", "press", "select", "scroll", "find",
                          "back", "wait", "open_app", "switch", "check_file", *_FILE_OPS,
                          *_LOOK}),
}  # fmt: skip
_SYSTEM: dict[SurfaceName, str] = {"browser": SYSTEM, "desktop": DESKTOP_SYSTEM}
# controls whose click changes a value (a switch, a box, an option) — matters in Windows Settings
_VALUE_TAGS = {"check box", "radio button", "slider", "combo box", "spinner"}
_RISK_FLAG = {
    "irreversible": "agent_irreversible",
    "payment": "agent_payment",
    "credentials": "agent_credentials",
}
PAGE_CHANGING = {"click", "double_click", "navigate", "back", "press", "open_app", "switch"}
# scrolling again and again usually means looking for something that isn't there
SCROLL_HINT = 3  # in a row → remind the planner of find / another way
SCROLL_STOP = 6  # in a row → stop


@dataclass
class RunStep:
    """One step of a run, by what it acted on (for learning a skill)."""

    op: str
    tag: str = ""
    name: str = ""
    text: str = ""
    key: str = ""
    url: str = ""
    ok: bool = True
    kind: str = ""
    flags: list[str] = field(default_factory=list)


@dataclass
class AgentOutcome:
    ok: bool
    message: str
    error: str | None = None
    steps: int = 0
    actions: int = 0
    ms: float = 0.0
    usage: Usage = field(default_factory=Usage)


def describe(act: AgentAction, el: Element | None) -> str:
    target = f'"{el.name}"' if el and el.name else (f"[{act.element}]" if act.element else "")
    if act.op == "type":
        return f'type "{act.text}" into {target}'.strip()
    if act.op == "press":
        return f"press {act.key or 'Enter'}" + (f" in {target}" if target else "")
    if act.op == "select":
        return f'select "{act.text}" in {target}'
    if act.op == "navigate":
        return f"open {act.url}"
    if act.op == "scroll":
        return f"scroll {act.text or 'down'}"
    if act.op == "find":
        return f'find "{act.text or ""}"'
    if act.op in ("open_app", "switch", "check_file", "open_file"):
        return f'{act.op.replace("_", " ")} "{act.text or ""}"'
    if act.op == "attach":
        return f'attach "{act.text or ""}"' + (f" with {target}" if target else "")
    if act.op == "wait_download":
        return "wait for the download"
    if act.op in _TAB_OPS:
        what = act.url or act.text or (target if target else "")
        return f"{act.op.replace('_', ' ')} {what}".strip()
    return f"{act.op.replace('_', ' ')} {target}".strip()


# a sentence end followed straight away by code-like leftovers: '… one stop.}', '… prices.](#)',
# '… its box.», ».replace("»","")' — models sometimes append these; they must not be spoken
# '<' — '… App.</br>'; a quote then a comma — "… Nepal.','risk_hint':'none'" (JSON leaking in)
_DEBRIS = re.compile(r"""([.!?])\s*(?:[\]}»«`=#\\<]|['"]\s*,).*$""", re.S)


def _pages_seen(visits: dict[str, list[Any]]) -> list[str]:
    """Pages looked at in this task, with how often — "(seen 2×)" means you've been there before."""
    lines = []
    for url, (title, times) in list(visits.items())[-10:]:
        again = f" (seen {times}×)" if times > 1 else ""
        lines.append(f"{title[:60]} — {_short(url)}{again}")
    return lines


def _short(location: str) -> str:
    """An address short enough for the history, keeping what tells pages apart (path + query)."""
    rest = location.split("://", 1)[-1]
    return rest if len(rest) <= 110 else rest[:70] + "…" + rest[-35:]


VISION_NOTE = """

You are also SEEING THE SCREEN: the picture shows where you work, with {n} numbered boxes on \
what can be clicked. In this step, `element` means a BOX NUMBER from the picture (not a [number] \
from the text list), and every click / double_click / type on a box must set `label` to what that \
box shows (its words, or what the icon / picture is, e.g. "Buy now", "black colour swatch"). Use \
the picture for what the text can't show; press / scroll / back / wait work as usual."""


def box_action(
    act: AgentAction, look: Look, snap: Snapshot, surface: SurfaceName
) -> tuple[Action | None, str | None]:
    """A step on a numbered box of a screenshot → a click / type at its centre. Its label (what
    the box shows, read by the vision model) is what the safety policy judges."""
    if act.op not in ("click", "double_click", "type"):
        return None, f"{act.op} on a picture box isn't possible — use click or type"
    point = look.click_point(int(act.element or 0))
    if point is None:
        return None, f"there is no box {act.element} in the picture"
    label = (act.label or "").strip()
    flags = {_RISK_FLAG[act.risk_hint]} if act.risk_hint in _RISK_FLAG else set()
    args: dict[str, Any] = {"x": round(point[0], 1), "y": round(point[1], 1)}
    if act.op == "type":
        args["text"] = act.text or ""
    if surface == "desktop":
        args["surface"] = "desktop"
    kind = "type_at" if act.op == "type" else "click_at"
    where = f"in {snap.title}" if surface == "desktop" else (
        "on " + (snap.url.split("/")[2] if "://" in snap.url else snap.url))  # fmt: skip
    what = f'"{label}"' if label else f"box {act.element} on the screen"
    verb = {"click": "click", "double_click": "double-click", "type": "type into"}[act.op]
    return Action(kind=kind, target=label, args=args, description=f"{verb} {what} {where}",
                  flags=flags, page_url=snap.url), None  # fmt: skip


def tidy(message: str) -> str:
    """Drop debris a model sometimes leaves after the last sentence (it would be read aloud)."""
    text = _DEBRIS.sub(r"\1", message.strip())
    while text.endswith(")") and text.count(")") > text.count("("):  # '… 125 percent.)'
        text = text[:-1].rstrip()
    return text.rstrip('}]"').rstrip() or message


_ACTIVATING = {"enter", "return", "space", " "}


def _activates(key: str) -> bool:
    """Does this key press the focused control (Enter, Space, Ctrl+Enter …)?"""
    return key.lower().replace(" ", "").split("+")[-1] in _ACTIVATING or key.strip() == " "


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_FILE_NAME = re.compile(
    r"[\w ()\-.,']+\.(?:pdf|docx?|xlsx?|pptx?|csv|txt|jpe?g|png|gif|zip|rar|mp[34]|mov|wav)\b",
    re.IGNORECASE,
)
_FIELD_TAGS = ("input", "textarea", "edit", "combo box", "select", "document")


def _file_name(match: str) -> str:
    """'gmail.com Mopidi_Resume 8.pdf' → 'Mopidi_Resume 8.pdf': the name is the words right before
    the extension, back to the last word that is itself dotted (a domain, another file)."""
    words = match.split()
    keep = [words[-1]]
    for word in reversed(words[:-1]):
        if "." in word or "@" in word or ":" in word:
            break
        keep.insert(0, word)
    return " ".join(keep).strip(" ,'")


def _focused(snap: Snapshot) -> Element | None:
    return next((e for e in snap.elements if e.focused), None)


def read_back(snap: Snapshot, el: Element | None) -> str:
    """For a Send / Submit question: what goes out — recipients, filled-in fields, attachments —
    read from the form / dialog the button is in (one compose window, not another one)."""
    group = el.group if el is not None else 0
    in_group = [e for e in snap.elements if e.group == group] if group else snap.elements
    fields = [f'{e.name}: "{e.value}"' for e in in_group
              if e.value and not e.password and not e.otp and e.tag in _FIELD_TAGS]  # fmt: skip
    parts = fields[:4]
    if group and group in snap.groups:
        text = snap.groups[group]
        typed = " ".join(fields)
        emails = [m for m in dict.fromkeys(_EMAIL.findall(text)) if m not in typed]
        if emails:
            parts.append("to " + ", ".join(emails[:5]))
        files = list(dict.fromkeys(_file_name(m) for m in _FILE_NAME.findall(text)))
        if files:
            parts.append("attachments: " + ", ".join(files[:5]))
        elif re.search(r"\battach", text, re.IGNORECASE) or any(
            "attach" in e.name.lower() for e in in_group
        ):
            parts.append("no attachments")
    return "; ".join(parts)


def to_action(
    act: AgentAction, snap: Snapshot, surface: SurfaceName = "browser"
) -> tuple[Action | None, str | None]:
    """The planner's step → an Action for ActionRunner (label = what the policy reads)."""
    if act.op not in OPS[surface]:
        return None, f"'{act.op}' can't be done here ({surface})"
    el = snap.by_id(act.element) if act.element is not None else None
    # in an app, "type" without a control types on the keyboard where the cursor is
    needs_element = ("click", "double_click", "select") + (
        ("type",) if surface == "browser" else ()
    )
    if (act.op in needs_element or act.element is not None and act.op == "type") and el is None:
        return None, f"element [{act.element}] isn't on the page"
    if act.op in ("open_app", "switch", "check_file") and not (act.text or "").strip():
        return None, f"{act.op} needs the app or window name in text"
    if act.op in ("attach", "open_file") and not (act.text or "").strip():
        return None, f"{act.op} needs the file in text (a full path or a description)"
    if act.op == "navigate" and not (act.url or "").startswith(("http://", "https://")):
        return None, "navigate needs a full http(s) URL"
    if act.op == "private_window" and not (act.url or act.text or "").startswith(_WEB):
        return None, "private_window needs a full http(s) URL"
    if act.op == "new_tab" and act.element is None and not (act.url or "").startswith(_WEB):
        return None, "new_tab needs a link [number] or a full http(s) URL"
    if act.op == "switch_tab" and not (act.text or "").strip():
        return None, "switch_tab needs the tab's [number], title or site in text"
    if act.op in ("press", "type") and el is None:  # keys go to whatever is focused: judge that
        el = next((e for e in snap.elements if e.focused), None)
    if act.op == "press" and el is not None and not _activates(act.key or "Enter"):
        el = None  # Escape / Tab / arrows on a focused "Send" button don't send

    flags = set()
    if el is not None and el.password:
        flags.add("password_field")
    if el is not None and el.otp:
        flags.add("otp_field")
    if act.risk_hint in _RISK_FLAG:  # the planner's own risk judgement — can only raise risk
        flags.add(_RISK_FLAG[act.risk_hint])
    changes_value = act.op in ("type", "select") or (
        act.op in ("click", "press") and el is not None
        and (el.tag in _VALUE_TAGS or el.checked is not None and el.tag != "list item")
    )  # fmt: skip
    if changes_value:
        flags.add("value_change")  # the policy cares where: e.g. in Windows Settings → confirm

    args: dict[str, Any] = {}
    if act.element is not None and snap.by_id(act.element) is not None:
        args["element"] = act.element
    if act.op in ("type", "select", "find"):
        args["text"] = act.text or ""
    if act.op == "press":
        args["key"] = act.key or "Enter"
    if act.op == "scroll":
        args["direction"] = act.text or "down"
    if surface == "desktop":
        args["surface"] = "desktop"  # routed to the desktop executor
    if act.op == "navigate":
        target = act.url or ""
    elif act.op in ("new_tab", "private_window"):
        target = act.url or act.text or (el.name if el else "")
    elif act.op in ("switch_tab", "close_tab"):
        target = (act.text or "").strip()
    elif act.op in ("open_app", "switch", "check_file", "attach", "open_file"):
        target = (
            act.text or ""
        ).strip()  # attach / open: resolved to a real file before the policy
    else:
        target = el.name if el else ""
    if surface == "desktop":
        where = f"in {snap.title}"  # the window, as the user sees it ("in Settings")
    else:
        where = "on " + (snap.url.split("/")[2] if "://" in snap.url else snap.url)
    now = f' (now "{el.value}")' if el is not None and el.value and act.op != "type" else ""
    return (
        Action(
            kind=_KIND[act.op],
            target=target,
            args=args,
            description=f"{describe(act, el)}{now} {where}",
            flags=flags,
            page_url=snap.url,
        ),
        None,
    )


class TaskAgent:
    """The loop, on whichever surface the task needs (the router says: browser or desktop)."""

    def __init__(
        self,
        planners: list[LLMProvider],
        surfaces: dict[SurfaceName, Surface],
        run_action: RunAction,
        settings: AgentSettings,
        trace_dir: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        eyes: Eyes | None = None,
    ) -> None:
        self.planners = planners
        self.eyes = eyes  # the vision tier: a screenshot when text isn't enough (None = text only)
        self.last_run: list[RunStep] = []  # the last run's steps — a skill is learned from them
        self.last_where: SurfaceName = "browser"
        self.surfaces = surfaces
        self.run_action = run_action  # ActionRunner.run_action — every step is risk-checked
        self.settings = settings
        self.trace_dir = trace_dir
        self.clock = clock

    async def run(self, goal: str, where: SurfaceName = "browser") -> AgentOutcome:
        surface = self.surfaces.get(where) or next(iter(self.surfaces.values()))
        self.last_run, self.last_where = [], surface.name
        started = self.clock()
        history: list[str] = []
        previous: Snapshot | None = None  # the last look, to tell the model what changed
        look_next = False  # the agent asked to see the screen (vision) on its next look
        looks: dict[str, int] = {}
        last_plan: list[str] = []  # the agent's own plan, shown back to it every step
        last_blocker: str | None = None  # screenshots asked for, per page
        visits: dict[str, list[Any]] = {}  # page → [title, times seen] — to stop re-checking
        thoughts: list[str] = []  # the planner's notes ("Airdopes 141 at ₹1,199") — for a pay step
        seen: set[tuple[Any, ...]] = set()
        failures: dict[tuple[str, str, str], int] = {}  # same action, same reason → how often
        scrolls = 0  # scroll actions in a row
        attached: list[str] = []  # files attached in this task — named in Send / Submit questions
        confirmed_done: set[tuple[str, str]] = set()  # steps the user said yes to, that ran
        heard: set[str] = set()  # page announcements already there — old news, not shown again
        usage = Usage()
        actions_done = 0
        trace = Trace(goal, self.trace_dir)

        def outcome(
            ok: bool, message: str, error: str | None = None, steps: int = 0
        ) -> AgentOutcome:
            result = AgentOutcome(ok, tidy(message), error, steps, actions_done,
                                  (self.clock() - started) * 1000, usage)  # fmt: skip
            trace.finish(result)
            return result

        for step in range(1, self.settings.max_steps + 1):
            if self.clock() - started > self.settings.time_budget_s:
                return outcome(False, "That's taking too long, so I stopped.", "time_budget", step)

            try:
                blocked = await surface.blocked()
                if blocked is not None:
                    return outcome(False, blocked.observation or "A human check needs you.",
                                   blocked.error or "captcha", step)  # fmt: skip
                snap = await surface.observe(
                    self.settings.max_elements, self.settings.max_text_chars
                )
            except BrowserUnavailable as exc:
                return outcome(False, f"I can't use the Jarvis Chrome window: {exc}",
                               "browser_unavailable", step)  # fmt: skip
            # screen-reader announcements: only NEW ones ("Message sent" after our Send) — what a
            # site announced before this task or in an earlier look is no evidence of anything
            fresh = [a for a in snap.announcements if a not in heard] if step > 1 else []
            heard.update(snap.announcements)
            snap = snap.model_copy(update={"announcements": fresh})
            where_before = surface.location()
            # vision: when the agent asked to look, or an app window shows nothing readable
            look = None
            blank_app = surface.name == "desktop" and not snap.elements and snap.url != "app://none"
            if self.eyes is not None and (look_next or blank_app):
                try:
                    look = await self.eyes.look(surface)
                except Exception:
                    log.warning("couldn't look at the screen", exc_info=True)
                    history.append("look: not possible right now — use the text view")
            look_next = False

            try:
                plan, step_usage = await self._plan(goal, step, history, snap, surface.name,
                                                    _pages_seen(visits),
                                                    what_changed(previous, snap), look,
                                                    last_plan, last_blocker)  # fmt: skip
            except LLMUnavailable as exc:
                return outcome(
                    False, f"I need a cloud model for that, and {exc}.", "no_planner", step
                )
            previous = snap
            usage = _add(usage, step_usage)
            trace.step(step, snap, plan)
            thoughts.append(plan.thought)
            last_plan, last_blocker = plan.plan or last_plan, plan.blocker  # carried to next step
            visit = visits.setdefault(snap.url, [snap.title, 0])
            visit[1] += 1

            batch = plan.actions[:MAX_ACTIONS]
            if any(a.op == "look" for a in batch):
                look_next = True  # next round: a screenshot with numbered boxes
                batch = [a for a in batch if a.op != "look"]
            for i, act in enumerate(batch):
                if look is not None and act.element is not None:
                    action, problem = box_action(act, look, snap, surface.name)
                else:
                    action, problem = to_action(act, snap, surface.name)
                if action is None:
                    history.append(f"{act.op}: not done — {problem}")
                    break
                if (
                    look is None
                    and i > 0
                    and act.element is not None
                    and not await surface.still_there(act.element)
                ):
                    break  # the page re-drew that part: the number is stale — look again
                # typing or clicking may pop up suggestions / a dropdown / a popup that must be
                # dealt with first — so if more actions follow, check whether the page changed
                watch = i < len(batch) - 1
                before = await surface.size() if watch or act.op == "press" else 0
                loc_before = surface.location()
                if (action.kind, action.target) in confirmed_done:
                    # a second Send / Submit / Pay would do it twice: never — say so instead
                    why = (f"I already did that once in this task ({action.target}), so I won't "
                           "do it again — please check whether it went through.")  # fmt: skip
                    return outcome(False, why, "repeat_blocked", step)
                if action.kind in ("click", "double_click", "press_key"):
                    # if this turns out to be Send / Submit, the question says what goes with it
                    shown = read_back(snap, snap.by_id(act.element) or _focused(snap))
                    extra = [shown] if shown else []
                    if attached and "attachments:" not in shown:
                        extra.append(f"attached in this task: {', '.join(attached)}")
                    if extra:
                        action = action.model_copy(update={
                            "description": f"{action.description} — {'; '.join(extra)}",
                        })  # fmt: skip
                    # if this places an order, the order reader checks it against the request
                    notes = " | ".join(thoughts[-6:])[-900:]
                    action = action.model_copy(
                        update={"args": {**action.args, "task": goal, "notes": notes}}
                    )
                result = await self.run_action(action)
                if result.ok and result.data.get("confirmed"):
                    confirmed_done.add((action.kind, action.target))
                if result.ok and result.data.get("attached"):
                    attached.append(Path(str(result.data["attached"])).name)
                actions_done += 1
                status = "ok" if result.ok else f"failed: {result.observation or result.error}"
                if result.ok and result.data.get("asked"):  # what the user was asked, and said
                    status = (
                        f'{status} (you asked the user "{result.data["asked"]}" and they said yes)'
                    )
                if result.ok and act.op == "press":
                    await surface.settle()
                    if surface.location() == loc_before and await surface.size() == before:
                        # e.g. Escape on a notice that doesn't close that way: don't press it again
                        status = "ok, but nothing on the page changed"
                if result.data.get(
                    "page_notes"
                ):  # the page's own question / notice, and the answer
                    status = f"{status} ({result.data['page_notes']})"
                if result.ok and (
                    act.op in ("find", "open_app", "switch", "check_file", *_FILE_OPS, *_TAB_OPS)
                    or "new tab" in result.observation  # a click that opened (and moved to) a tab
                ):
                    status = f"ok: {result.observation}"
                if result.ok and surface.location() != loc_before:
                    status = f"{status} → now on {_short(surface.location())}"
                history.append(f"{describe(act, snap.by_id(act.element))} → {status}")
                trace.action(action, result, snap.by_id(act.element))
                el = snap.by_id(act.element)
                self.last_run.append(RunStep(
                    # a control's name only if there was one (a step's own label isn't a name)
                    op=act.op, tag=el.tag if el else "", name=el.name if el else "",
                    text=act.text or "", key=act.key or "", url=act.url or "",
                    ok=result.ok, kind=action.kind, flags=sorted(action.flags),
                ))  # fmt: skip
                scrolls = scrolls + 1 if act.op == "scroll" else 0
                if scrolls >= SCROLL_STOP:
                    why = "I scrolled through the page again and again without finding what I "
                    return outcome(False, why + "needed, so I stopped.", "stuck", step)
                if scrolls == SCROLL_HINT:
                    history.append(f"(you have scrolled {scrolls} times in a row: if you are "
                                   "looking for something, use find; if it isn't on the page, do "
                                   "it another way or tell the user)")  # fmt: skip
                if result.error in _STOP:
                    why = result.observation or "Stopped."
                    if result.error == "user_declined":
                        why = "Okay, I didn't do it."
                    return outcome(False, why, result.error, step)
                if not result.ok:
                    key = (action.kind, action.target, result.observation or result.error or "")
                    failures[key] = failures.get(key, 0) + 1
                    if failures[key] >= 2:  # it already tried this and was told why: don't loop
                        why = result.observation or result.error or "it didn't work"
                        return outcome(False, f"I'm stuck: {why}", "stuck", step)
                    break
                if act.op in PAGE_CHANGING and surface.location() != where_before:
                    break  # the page changed: the remaining numbers are stale — look again
                if watch:
                    await surface.settle()
                    if await surface.size() != before:
                        break  # something appeared or went away: look before going on

            if plan.status == "done":
                trace.evidence(snap)  # what the screen showed when it worked — for skills
                return outcome(True, plan.message or "Done.", None, step)
            if plan.status == "ask":
                return outcome(False, plan.message or "I need your help with that.", "ask", step)
            if plan.status == "fail":
                return outcome(False, plan.message or "I couldn't do that.", "failed", step)

            # compare by WHAT is acted on (labels), not element numbers — numbers are reused on
            # every snapshot, so "click [7] Mumbai" and "click [7] London" must not look the same.
            # The same action on a page that changed is progress ("Next month" until January),
            # on an unchanged page it's a loop.
            acted_on = tuple(describe(a, snap.by_id(a.element)) for a in plan.actions)
            signature = (snap.fingerprint(), acted_on)
            moved_on = surface.location() != where_before  # this time it took us somewhere
            only_looking = bool(plan.actions) and all(a.op == "look" for a in plan.actions)
            if only_looking:  # looking again after new information isn't a loop — up to 3 times
                looks[snap.url] = looks.get(snap.url, 0) + 1
                if looks[snap.url] > MAX_LOOKS_PER_PAGE:
                    return outcome(False, "I looked at this screen several times and still "
                                   "couldn't find a way, so I stopped.", "stuck", step)  # fmt: skip
                continue
            if signature in seen and not moved_on:
                return outcome(False, "I'm going round in circles, so I stopped.", "stuck", step)
            seen.add(signature)

        return outcome(False, "That needed more steps than I'm allowed, so I stopped.",
                       "max_steps", self.settings.max_steps)  # fmt: skip

    async def _plan(
        self, goal: str, step: int, history: list[str], snap: Snapshot,
        surface: SurfaceName = "browser", seen_pages: list[str] | None = None,
        changed: list[str] | None = None, look: Look | None = None,
        last_plan: list[str] | None = None, last_blocker: str | None = None,
    ) -> tuple[AgentStep, Usage]:  # fmt: skip
        prompt = user_prompt(goal, step, self.settings.max_steps, history, snap,
                             seen=seen_pages or [], changed=changed or [],
                             plan=last_plan or [], blocker=last_blocker)  # fmt: skip
        image = None
        if look is not None:
            prompt += VISION_NOTE.format(n=len(look.boxes))
            image = look.picture
        reasons = []
        # the main planner gets a second try (a dropped connection, a malformed reply happen);
        # the others are fallbacks
        attempts = self.planners[:1] * 2 + self.planners[1:]
        for planner in attempts:
            try:
                reply = await planner.decide(_SYSTEM[surface], prompt, AgentStep, max_tokens=1200,
                                             **({"image": image} if image else {}))  # fmt: skip
            except LLMUnavailable as exc:
                reasons.append(str(exc))
                continue
            if isinstance(reply.decision, AgentStep):
                log.info("agent step %d via %s (%.0f ms): %s", step, planner.name, reply.ms,
                         reply.decision.thought)  # fmt: skip
                return reply.decision, reply.usage
            reasons.append(f"{planner.name} gave an unreadable plan")
        # say why the MAIN planner failed — not "no key" for a fallback that was never set up
        raise LLMUnavailable(reasons[0] if reasons else "no cloud model is configured")


class BrowserAgent(TaskAgent):
    """The agent with only the browser (Phase 11 wiring, kept for tests and evals)."""

    def __init__(
        self,
        planners: list[LLMProvider],
        browser: BrowserExecutor,
        run_action: RunAction,
        settings: AgentSettings,
        trace_dir: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        eyes: Eyes | None = None,
    ) -> None:
        super().__init__(planners, {"browser": BrowserSurface(browser)}, run_action, settings,
                         trace_dir, clock, eyes)  # fmt: skip


def _add(a: Usage, b: Usage) -> Usage:
    return Usage(
        a.input_tokens + b.input_tokens,
        a.cache_read_tokens + b.cache_read_tokens,
        a.cache_write_tokens + b.cache_write_tokens,
        a.output_tokens + b.output_tokens,
    )


class Trace:
    """Agent run log (redacted, no screenshots) → logs/agent/<time>.jsonl — Phase 12 learns skills
    from these. Never breaks a run."""

    def __init__(self, goal: str, directory: Path | None) -> None:
        self.path: Path | None = None
        if directory is None:
            return
        try:
            directory.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
            self.path = directory / f"{stamp}.jsonl"
            self._write({"event": "goal", "goal": redact(goal)})
        except OSError:
            self.path = None

    def step(self, n: int, snap: Snapshot, plan: AgentStep) -> None:
        self._write({"event": "step", "n": n, "url": snap.url, "elements": len(snap.elements),
                     "thought": redact(plan.thought), "status": plan.status,
                     "plan": [redact(p) for p in plan.plan],
                     "blocker": redact(plan.blocker) if plan.blocker else None,
                     "actions": [a.model_dump() for a in plan.actions]})  # fmt: skip

    def action(self, action: Action, result: ActionResult, element: Element | None = None) -> None:
        """The control by what it is (type, name, value) — not only its number, which changes every
        look: Phase 12 skills find it again by these."""
        control = (
            {"tag": element.tag, "role": element.role, "name": redact(element.name),
             "value": "" if element.password else redact(element.value)}
            if element is not None else None
        )  # fmt: skip
        args = {k: v for k, v in action.args.items() if k in ("text", "key", "direction", "path")}
        self._write({"event": "action", "kind": action.kind, "target": redact(action.target),
                     "control": control, "args": {k: redact(str(v)) for k, v in args.items()},
                     "ok": result.ok, "error": result.error})  # fmt: skip

    def evidence(self, snap: Snapshot) -> None:
        """The screen at the moment the goal was reached (a skill checks for this on replay)."""
        self._write({"event": "evidence", "url": snap.url, "title": redact(snap.title),
                     "text": redact(snap.text[:300])})  # fmt: skip

    def finish(self, outcome: AgentOutcome) -> None:
        self._write({"event": "end", "ok": outcome.ok, "error": outcome.error,
                     "message": redact(outcome.message), "steps": outcome.steps,
                     "actions": outcome.actions, "ms": round(outcome.ms)})  # fmt: skip

    def _write(self, record: dict[str, Any]) -> None:
        if self.path is None:
            return
        try:
            record = {"ts": round(time.time(), 3), **record}
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            log.debug("agent trace write failed", exc_info=True)


def default_trace_dir() -> Path:
    return data_dir() / "logs" / "agent"
