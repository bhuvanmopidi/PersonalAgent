"""What the LLM returns when it routes a command: one structured decision."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Slot(BaseModel):
    name: str
    value: str


class RouterDecision(BaseModel):
    """run_intent: do one of the listed commands · answer: reply in words (questions) ·
    clarify: ask one short question back · unsupported: can't do that yet (say why)."""

    kind: Literal["run_intent", "answer", "clarify", "unsupported"]
    intent: str | None = Field(default=None, description="intent name, only for run_intent")
    # a list, not a dict: keeps the JSON schema strict-friendly for every provider
    slots: list[Slot] = Field(default_factory=list, description="only for run_intent")
    text: str | None = Field(
        default=None, description="the answer / question / reason, for the other kinds"
    )

    def slot_dict(self) -> dict[str, str]:
        return {s.name: s.value for s in self.slots if s.value.strip()}
