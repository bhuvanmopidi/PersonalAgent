"""Tier-1 router: match normalized text against regex intents from config/intents.yaml."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from jarvis.config import CONFIG_DIR
from jarvis.models import Intent

INTENTS_PATH = CONFIG_DIR / "intents.yaml"

_PREFIX = re.compile(
    r"^(?:(?:hey|ok|okay)\s+)?(?:jarvis\s+)?(?:(?:please|can you|could you|would you|will you)\s+)*"
)
_SUFFIX = re.compile(r"(?:\s+(?:please|for me|now))+$")
# A slot that only says "it" / "that" refers to something earlier — the rules can't know what.
# Don't act on it ("open it" must never open an app called "it"); the LLM resolves it or asks.
# A slot starting with one of these points at something ("that insta thing", "it a bit quicker",
# "this video from the beginning") — the catch-all rules can't know what; leave it to the LLM.
_POINTERS = {"it", "this", "that", "these", "those", "them"}
VAGUE = {"it", "that", "this", "them", "those", "these", "one", "the one", "that one", "this one"}


class ActionTemplate(BaseModel):
    kind: str
    target: str = ""
    args: dict[str, Any] = Field(default_factory=dict)


class IntentSpec(BaseModel):
    name: str
    patterns: list[str]
    action: ActionTemplate
    defaults: dict[str, str] = Field(default_factory=dict)
    description: str = ""
    # what to say on success: the description up front, the result afterwards, or nothing
    speak: Literal["description", "result", "none"] = "description"
    # for the LLM catalog (Phase 7): sample phrasings, what each slot means, whether it's offered
    examples: list[str] = Field(default_factory=list)
    slots: dict[str, str] = Field(default_factory=dict)
    optional: list[str] = Field(default_factory=list)  # slots the action can do without
    llm: bool = True


class IntentFile(BaseModel):
    intents: list[IntentSpec] = Field(default_factory=list)


def normalize(text: str) -> str:
    """'Hey Jarvis, please open Spotify!' → 'open spotify'."""
    t = text.lower()
    t = re.sub(r"[,!?]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip().rstrip(".").strip()
    t = _PREFIX.sub("", t)
    return _SUFFIX.sub("", t).strip()


class RegexRouter:
    def __init__(self, specs: list[IntentSpec]) -> None:
        self.specs = {s.name: s for s in specs}
        # order matters: first matching pattern wins, so specific intents go first in the YAML
        self._compiled = [(s, re.compile(p)) for s in specs for p in s.patterns]

    @classmethod
    def from_yaml(cls, path: Path = INTENTS_PATH) -> RegexRouter:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(IntentFile.model_validate(data).intents)

    def route(self, text: str) -> Intent | None:
        normalized = normalize(text)
        for spec, pattern in self._compiled:
            match = pattern.fullmatch(normalized)
            if match:
                slots = {k: v.strip() for k, v in match.groupdict().items() if v}
                if any(v in VAGUE or v.split()[0] in _POINTERS for v in slots.values()):
                    return None
                return Intent(name=spec.name, args={**spec.defaults, **slots}, source="regex")
        return None
