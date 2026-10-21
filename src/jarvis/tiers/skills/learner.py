"""Learning a skill from a run that worked: one model call turns the steps into a recipe with slots,
then the recipe is checked — filling the slots back in must give exactly what was done.

Never learned from: runs with a password / code / card step or a hand-off, failed or cancelled
runs, and one-off answers (reading something off a page has nothing worth repeating).
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, Field

from jarvis.planner.llm import LLMProvider, LLMUnavailable
from jarvis.telemetry.logging import get_logger
from jarvis.tiers.skills.store import Skill, SkillStep, fill, slug
from jarvis.tiers.structured.agent import RunStep

log = get_logger(__name__)

# a run with any of these isn't saved: those steps are always the user's, live
_NEVER = {"password_field", "otp_field", "agent_credentials", "agent_payment", "captcha_detected"}
_REPLAYABLE = {"click", "double_click", "type", "press", "select", "scroll", "navigate", "back",
               "open_app", "switch", "new_tab", "switch_tab", "attach", "wait"}  # fmt: skip


class SlotDef(BaseModel):
    name: str = Field(description="short slot name, e.g. song, city, recipient")
    value: str = Field(description="its value in THIS run, exactly as it appears in the steps")
    about: str = Field(description="what it is, a few words")


class LearnedStep(BaseModel):
    step: int = Field(description="the number of the step in the run that this keeps")
    name: str = Field(
        default="", description="the control's name, with {slot} where a slot's value is"
    )
    text: str = Field(default="", description="the typed / chosen text, with {slot}")
    url: str = Field(default="", description="the address, with {slot}")


class LearnedSkill(BaseModel):
    worth_saving: bool = Field(description="false if this is a one-off (an answer read off a page)")
    name: str = Field(description="the kind of task, with slots: e.g. 'play {song} on Spotify'")
    description: str = Field(description="one line: what it does")
    slots: list[SlotDef] = Field(default_factory=list)
    steps: list[LearnedStep] = Field(
        default_factory=list, description="only the steps needed, in order (no retries / dead ends)"
    )
    check: str = Field(default="", description="words the screen shows when done, with {slot}")
    done_message: str = Field(default="", description="what to tell the user, with {slot}")


SYSTEM = """You turn a task that a computer agent just did successfully into a reusable recipe.

You get the user's request and the numbered steps the agent took (what it clicked / typed, on \
which control). Decide which values come from the request and would change next time (a song, \
city, date, recipient, product, search words) — those are slots. Then write:
- name: the kind of task with {slot} placeholders ("play {song} on Spotify")
- the steps needed, in order, by their number — leave out retries, dead ends and anything that \
failed; in each kept step put {slot} where a slot's value appears in the control name, typed \
text or address (keep the rest exactly as it was)
- check: a few words the screen shows when it worked, with {slot} (or empty)
- done_message: one short sentence for the user, with {slot}
worth_saving = false when the task was a one-off question answered from a page, when nothing \
in it would be worth repeating, or when it only worked because what was wanted happened to be on \
the screen already (picked from a recent / suggested list without searching or navigating to it) \
— next time, with another value, it won't be there."""


def describe_run(goal: str, steps: Sequence[RunStep]) -> str:
    lines = [f"Request: {goal}", "Steps:"]
    for n, s in enumerate(steps, 1):
        parts = [f"{n}. {s.op}"]
        if s.name:
            parts.append(f'{s.tag or "control"} "{s.name}"')
        if s.text:
            parts.append(f'text "{s.text}"')
        if s.key:
            parts.append(f"key {s.key}")
        if s.url:
            parts.append(f"url {s.url}")
        parts.append("ok" if s.ok else "FAILED")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def learnable(steps: Sequence[RunStep]) -> str | None:
    """Why this run can't become a skill, or None."""
    if not steps:
        return "no steps"
    for s in steps:
        if _NEVER & set(s.flags):
            return f"a step needs the user ({', '.join(sorted(_NEVER & set(s.flags)))})"
    if not any(s.ok and s.op in _REPLAYABLE for s in steps):
        return "nothing to repeat"
    return None


def build(learned: LearnedSkill, steps: Sequence[RunStep], where: str) -> Skill | None:
    """The checked recipe — or None if it doesn't reproduce what was done."""
    values = {s.name: s.value for s in learned.slots}
    kept: list[SkillStep] = []
    last = 0
    for ls in learned.steps:
        if not 1 <= ls.step <= len(steps) or ls.step <= last:
            return None  # unknown or out-of-order step
        last = ls.step
        run = steps[ls.step - 1]
        if not run.ok or run.op not in _REPLAYABLE:
            continue
        # a template only where the step really had a value (no control → no name)
        name = (ls.name or run.name) if run.name else ""
        text = (ls.text or run.text) if run.text else ""
        url = (ls.url or run.url) if run.url else ""
        # filling the slots back in must give exactly what was done
        for template, original in ((name, run.name), (text, run.text), (url, run.url)):
            if original and fill(template, values).strip().lower() != original.strip().lower():
                log.info("skill rejected: %r doesn't give back %r", template, original)
                return None
        kept.append(SkillStep(op=run.op, tag=run.tag, name=name, text=text, key=run.key, url=url))
    if not kept:
        return None
    return Skill(id=slug(learned.name), name=learned.name, description=learned.description,
                 where="desktop" if where == "desktop" else "browser",
                 slots={s.name: s.about for s in learned.slots}, steps=kept,
                 check=learned.check, done_message=learned.done_message)  # fmt: skip


class Learner:
    def __init__(self, planners: Sequence[LLMProvider]) -> None:
        self.planners = list(planners)

    async def learn(self, goal: str, steps: Sequence[RunStep], where: str) -> Skill | None:
        why = learnable(steps)
        if why is not None:
            log.info("not learning a skill from %r: %s", goal, why)
            return None
        prompt = describe_run(goal, steps)
        for planner in self.planners:
            try:
                reply = await planner.decide(SYSTEM, prompt, LearnedSkill, max_tokens=1200)
            except LLMUnavailable as exc:
                log.info("skill learner: %s unavailable (%s)", planner.name, exc)
                continue
            learned = reply.decision
            if not isinstance(learned, LearnedSkill):
                continue
            if not learned.worth_saving:
                log.info("not learning %r: a one-off", goal)
                return None
            skill = build(learned, steps, where)
            if skill is not None:
                log.info("learned skill %s (%d steps)", skill.id, len(skill.steps))
            return skill
        return None
