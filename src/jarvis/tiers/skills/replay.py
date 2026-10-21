"""Replaying a skill: each step finds its control on the current screen by what it is (type +
name, slots filled in) and acts through the same ActionRunner / safety check as the agent — no
model call per step. Any step that can't be done stops the replay; the agent then takes over from
the current screen.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass

from rapidfuzz import fuzz

from jarvis.config import AgentSettings
from jarvis.telemetry.logging import get_logger
from jarvis.tiers.skills.store import Skill, SkillStep, fill
from jarvis.tiers.structured.agent import STOP_ERRORS, RunAction, to_action
from jarvis.tiers.structured.observer import Element, Snapshot
from jarvis.tiers.structured.schema import AgentAction
from jarvis.tiers.structured.surface import Surface

log = get_logger(__name__)

FIND_RETRY_S = 2.5  # the page may still be drawing: look again for this long
_NEEDS_CONTROL = {"click", "double_click", "type", "select", "attach"}
_NO_LOOK = {"open_app", "switch", "navigate", "new_tab", "switch_tab", "back", "wait"}


@dataclass
class ReplayOutcome:
    ok: bool
    message: str
    step: int = 0  # the step it stopped at (1-based), 0 if it finished
    error: str | None = None
    ms: float = 0.0


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w₹$.,@ ]+", " ", text.lower()).split())


def find_control(snap: Snapshot, tag: str, name: str, values: dict[str, str]) -> Element | None:
    """The control this step meant: same type; name equal, else containing it, else containing
    all its slot values; best fuzzy match wins."""
    wanted = _norm(name)
    if not wanted:
        return None
    same_type = [e for e in snap.elements if not tag or e.tag == tag] or list(snap.elements)
    exact = [e for e in same_type if _norm(e.name) == wanted]
    if exact:
        return exact[0]
    contains = [e for e in same_type if wanted in _norm(e.name)]
    if contains:
        return min(contains, key=lambda e: len(e.name))
    words = set(wanted.replace(",", " ").split())  # the same words in another order
    reordered = [e for e in same_type if set(_norm(e.name).replace(",", " ").split()) == words]
    if reordered:
        return reordered[0]
    slot_words = [_norm(v) for v in values.values() if v and _norm(v) in wanted]
    if slot_words:
        with_values = [e for e in same_type if all(w in _norm(e.name) for w in slot_words)]
        if with_values:
            return max(with_values, key=lambda e: fuzz.ratio(_norm(e.name), wanted))
    scored = [(fuzz.ratio(_norm(e.name), wanted), e) for e in same_type]
    best = max(scored, key=lambda x: x[0], default=(0, None))
    return best[1] if best[0] >= 88 else None


class Replayer:
    def __init__(self, run_action: RunAction, settings: AgentSettings) -> None:
        self.run_action = run_action
        self.settings = settings

    async def _look(self, surface: Surface) -> Snapshot:
        return await surface.observe(self.settings.max_elements, self.settings.max_text_chars)

    async def run(self, skill: Skill, values: dict[str, str], surface: Surface) -> ReplayOutcome:
        started = time.monotonic()

        def done(ok: bool, message: str, step: int = 0, error: str | None = None) -> ReplayOutcome:
            return ReplayOutcome(ok, message, step, error, (time.monotonic() - started) * 1000)

        missing = sorted(n for n in skill.slots if not values.get(n))
        if missing:
            return done(False, f"the skill needs {', '.join(missing)}", 0, "missing_slot")
        snap: Snapshot | None = None
        for n, step in enumerate(skill.steps, 1):
            log.info("replay %s: step %d %s at %.0f ms", skill.id, n, step.op,
                     (time.monotonic() - started) * 1000)  # fmt: skip
            # a fresh look before anything that acts on the screen (the policy judges it on
            # the live page); opening / switching / going somewhere doesn't need one
            if snap is None or step.op not in _NO_LOOK:
                snap = await self._look(surface)
            act, why = await self._step_action(step, values, snap, surface)
            if act is None:
                return done(False, why or "couldn't do a step", n, "not_found")
            snap = act[1]
            action, problem = to_action(act[0], snap, surface.name)
            if action is None:
                return done(False, problem or "couldn't do a step", n, "bad_step")
            result = await self.run_action(action)
            if result.error in STOP_ERRORS:
                why = (
                    "Okay, I didn't do it."
                    if result.error == "user_declined"
                    else result.observation
                )
                return done(False, why or "Stopped.", n, result.error)
            if not result.ok:
                return done(False, result.observation or result.error or "a step failed", n,
                            "step_failed")  # fmt: skip
        snap = await self._look(surface)
        check = fill(skill.check, values)
        if check and not self._shows(snap, check):
            return done(False, f'the screen doesn\'t show "{check}" at the end', len(skill.steps),
                        "check_failed")  # fmt: skip
        return done(True, fill(skill.done_message, values) or "Done.")

    async def _step_action(
        self, step: SkillStep, values: dict[str, str], snap: Snapshot, surface: Surface
    ) -> tuple[tuple[AgentAction, Snapshot] | None, str | None]:
        act = AgentAction(op=step.op, text=fill(step.text, values) or None,  # type: ignore[arg-type]
                          key=step.key or None, url=fill(step.url, values) or None)  # fmt: skip
        if step.op == "open_app" or step.op == "switch":
            act.text = fill(step.name, values) or act.text
        if not step.name and step.op not in ("click", "double_click", "select"):
            return (act, snap), None  # typed / pressed where the cursor was, as in the run
        if step.op not in _NEEDS_CONTROL and not (step.op == "press" and step.name):
            return (act, snap), None
        name = fill(step.name, values)
        deadline = time.monotonic() + FIND_RETRY_S
        while True:
            el = find_control(snap, step.tag, name, values)
            if el is None and step.op in ("type", "press"):
                # many fields change once clicked (a styled box becomes a text box): like a
                # person, type where the cursor now is
                el = next((e for e in snap.elements if e.focused), None)
            if el is not None:
                act.element = el.id
                return (act, snap), None
            if time.monotonic() >= deadline:
                return None, f'"{name}" isn\'t on the screen'
            await asyncio.sleep(0.4)
            snap = await self._look(surface)

    @staticmethod
    def _shows(snap: Snapshot, words: str) -> bool:
        seen = _norm(" ".join([snap.title, snap.text, *(e.name for e in snap.elements),
                               *(e.value for e in snap.elements)]))  # fmt: skip
        return all(w in seen for w in _norm(words).split())
