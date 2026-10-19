"""The general agent (Tier 3): does a task on any website — See → Think → Act → Check.

  observe the current tab (numbered elements + text)  →  the planner (Claude Haiku via OpenRouter)
  picks 1–5 actions  →  each action goes through ActionRunner (safety policy: confirm / hand off /
  refuse, never a bypass)  →  look again  →  until done / ask / fail / out of steps or time.

No site-specific code: the same loop works on YouTube, Amazon, Gmail, Wikipedia or a college portal.
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
from jarvis.models import Action, ActionResult
from jarvis.paths import data_dir
from jarvis.planner.llm import LLMProvider, LLMUnavailable, Usage
from jarvis.safety.redact import redact
from jarvis.telemetry.logging import get_logger
from jarvis.tiers.structured.observer import (
    Element,
    Snapshot,
    dom_size,
    observe,
    settle,
    still_there,
)
from jarvis.tiers.structured.prompts import SYSTEM, user_prompt
from jarvis.tiers.structured.schema import MAX_ACTIONS, AgentAction, AgentStep

if TYPE_CHECKING:
    from jarvis.executors.browser import BrowserExecutor

log = get_logger(__name__)

RunAction = Callable[[Action], Coroutine[Any, Any, ActionResult]]

# the user said no / cancelled, the policy refused, or the window is gone: stop, don't work around
_STOP = {"user_declined", "user_cancelled", "refused", "browser_closed", "browser_unavailable"}
_KIND = {
    "click": "click", "type": "type_text", "press": "press_key", "select": "select_option",
    "scroll": "scroll", "find": "find", "navigate": "navigate", "back": "go_back", "wait": "wait",
}  # fmt: skip
_RISK_FLAG = {
    "irreversible": "agent_irreversible",
    "payment": "agent_payment",
    "credentials": "agent_credentials",
}
PAGE_CHANGING = {"click", "navigate", "back", "press"}
# scrolling again and again usually means looking for something that isn't there
SCROLL_HINT = 3  # in a row → remind the planner of find / another way
SCROLL_STOP = 6  # in a row → stop


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
    return f"{act.op} {target}".strip()


# a sentence end followed straight away by code-like leftovers: '… one stop.}', '… prices.](#)',
# '… its box.», ».replace("»","")' — models sometimes append these; they must not be spoken
_DEBRIS = re.compile(r"([.!?])\s*[\]}»«`=#\\].*$", re.S)


def tidy(message: str) -> str:
    """Drop debris a model sometimes leaves after the last sentence (it would be read aloud)."""
    text = _DEBRIS.sub(r"\1", message.strip())
    return text.rstrip('}]"').rstrip() or message


def to_action(act: AgentAction, snap: Snapshot) -> tuple[Action | None, str | None]:
    """The planner's step → an Action for ActionRunner (label = what the policy reads)."""
    el = snap.by_id(act.element) if act.element is not None else None
    if act.op in ("click", "type", "select") and el is None:
        return None, f"element [{act.element}] isn't on the page"
    if act.op == "navigate" and not (act.url or "").startswith(("http://", "https://")):
        return None, "navigate needs a full http(s) URL"
    if act.op == "press" and el is None:  # a key goes to whatever is focused: judge that element
        el = next((e for e in snap.elements if e.focused), None)

    flags = set()
    if el is not None and el.password:
        flags.add("password_field")
    if el is not None and el.otp:
        flags.add("otp_field")
    if act.risk_hint in _RISK_FLAG:  # the planner's own risk judgement — can only raise risk
        flags.add(_RISK_FLAG[act.risk_hint])

    args: dict[str, Any] = {}
    if act.element is not None and snap.by_id(act.element) is not None:
        args["element"] = act.element
    if act.op in ("type", "select", "find"):
        args["text"] = act.text or ""
    if act.op == "press":
        args["key"] = act.key or "Enter"
    if act.op == "scroll":
        args["direction"] = act.text or "down"
    target = (act.url or "") if act.op == "navigate" else (el.name if el else "")
    site = snap.url.split("/")[2] if "://" in snap.url else snap.url
    return (
        Action(
            kind=_KIND[act.op],
            target=target,
            args=args,
            description=f"{describe(act, el)} on {site}",
            flags=flags,
            page_url=snap.url,
        ),
        None,
    )


class BrowserAgent:
    def __init__(
        self,
        planners: list[LLMProvider],
        browser: BrowserExecutor,
        run_action: RunAction,
        settings: AgentSettings,
        trace_dir: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.planners = planners
        self.browser = browser
        self.run_action = run_action  # ActionRunner.run_action — every step is risk-checked
        self.settings = settings
        self.trace_dir = trace_dir
        self.clock = clock

    async def run(self, goal: str) -> AgentOutcome:
        started = self.clock()
        history: list[str] = []
        seen: set[tuple[Any, ...]] = set()
        failures: dict[tuple[str, str, str], int] = {}  # same action, same reason → how often
        scrolls = 0  # scroll actions in a row
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

            page = await self.browser.agent_tab()
            blocked = await self.browser.captcha.check(page)
            if blocked is not None:
                return outcome(False, blocked.observation or "A human check needs you.",
                               blocked.error or "captcha", step)  # fmt: skip
            snap = await observe(page, self.settings.max_elements, self.settings.max_text_chars)

            try:
                plan, step_usage = await self._plan(goal, step, history, snap)
            except LLMUnavailable as exc:
                return outcome(
                    False, f"I need a cloud model for that, and {exc}.", "no_planner", step
                )
            usage = _add(usage, step_usage)
            trace.step(step, snap, plan)

            batch = plan.actions[:MAX_ACTIONS]
            for i, act in enumerate(batch):
                action, problem = to_action(act, snap)
                if action is None:
                    history.append(f"{act.op}: not done — {problem}")
                    break
                if i > 0 and act.element is not None and not await still_there(page, act.element):
                    break  # the page re-drew that part: the number is stale — look again
                # typing or clicking may pop up suggestions / a dropdown / a popup that must be
                # dealt with first — so if more actions follow, check whether the page changed
                watch = i < len(batch) - 1
                before = await dom_size(page) if watch or act.op == "press" else 0
                url_before = page.url
                result = await self.run_action(action)
                actions_done += 1
                status = "ok" if result.ok else f"failed: {result.observation or result.error}"
                if result.ok and act.op == "press":
                    await settle(page)
                    if page.url == url_before and await dom_size(page) == before:
                        # e.g. Escape on a notice that doesn't close that way: don't press it again
                        status = "ok, but nothing on the page changed"
                if result.ok and act.op == "find":
                    status = f"ok: {result.observation}"
                history.append(f"{describe(act, snap.by_id(act.element))} → {status}")
                trace.action(action, result)
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
                if act.op in PAGE_CHANGING and page.url != snap.url:
                    break  # the page changed: the remaining numbers are stale — look again
                if watch:
                    await settle(page)
                    if await dom_size(page) != before:
                        break  # something appeared or went away: look before going on

            if plan.status == "done":
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
            if signature in seen:
                return outcome(False, "I'm going round in circles, so I stopped.", "stuck", step)
            seen.add(signature)

        return outcome(False, "That needed more steps than I'm allowed, so I stopped.",
                       "max_steps", self.settings.max_steps)  # fmt: skip

    async def _plan(
        self, goal: str, step: int, history: list[str], snap: Snapshot
    ) -> tuple[AgentStep, Usage]:
        prompt = user_prompt(goal, step, self.settings.max_steps, history, snap)
        reasons = []
        for planner in self.planners:
            try:
                reply = await planner.decide(SYSTEM, prompt, AgentStep, max_tokens=1200)
            except LLMUnavailable as exc:
                reasons.append(str(exc))
                continue
            if isinstance(reply.decision, AgentStep):
                log.info("agent step %d via %s (%.0f ms): %s", step, planner.name, reply.ms,
                         reply.decision.thought)  # fmt: skip
                return reply.decision, reply.usage
            reasons.append(f"{planner.name} gave an unreadable plan")
        raise LLMUnavailable(reasons[-1] if reasons else "no cloud model is configured")


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
                     "actions": [a.model_dump() for a in plan.actions]})  # fmt: skip

    def action(self, action: Action, result: ActionResult) -> None:
        self._write({"event": "action", "kind": action.kind, "target": redact(action.target),
                     "ok": result.ok, "error": result.error})  # fmt: skip

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
