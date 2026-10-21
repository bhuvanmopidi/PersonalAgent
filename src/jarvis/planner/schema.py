"""What the LLM returns when it routes a command: one structured decision."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Slot(BaseModel):
    name: str
    value: str


class RouterDecision(BaseModel):
    """run_intent: do one of the listed commands · task: hand a goal to the general agent (on a
    website or in a Windows app) ·
    answer: reply in words (questions) · clarify: ask one short question back ·
    unsupported: can't do that yet (say why)."""

    kind: Literal["run_intent", "task", "answer", "clarify", "unsupported"]
    intent: str | None = Field(default=None, description="intent name, only for run_intent")
    # a list, not a dict: keeps the JSON schema strict-friendly for every provider
    slots: list[Slot] = Field(
        default_factory=list, description="for run_intent, and for a task that uses a skill"
    )
    text: str | None = Field(
        default=None, description="the answer / question / reason, for answer/clarify/unsupported"
    )
    goal: str | None = Field(
        default=None, description="only for task: the complete goal, with names from recent turns"
    )
    where: Literal["browser", "desktop"] | None = Field(
        default=None, description="only for task: browser (a website) or desktop (a Windows app)"
    )
    skill: str | None = Field(
        default=None,
        description="only for task: the id of a listed skill that does this kind of task (its "
        "slots go in `slots`); null if none fits",
    )

    def slot_dict(self) -> dict[str, str]:
        return {s.name: s.value for s in self.slots if s.value.strip()}
