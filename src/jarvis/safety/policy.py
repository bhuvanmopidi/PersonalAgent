"""Safety policy: decides whether an action may run, needs confirmation, or goes to the user.

Every action passes through SafetyPolicy.check() via ActionRunner — there is no bypass path.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from jarvis.config import CONFIG_DIR
from jarvis.models import Action, RiskLevel

DEFAULT_RULES_PATH = CONFIG_DIR / "safety.yaml"


class Decision(StrEnum):
    ALLOW = "allow"  # run it
    CONFIRM = "confirm"  # read back details, wait for yes
    HANDOFF = "handoff"  # user does it themselves
    REFUSE = "refuse"  # never run (e.g. payment with missing details)


class RiskLists(BaseModel):
    green: list[str] = Field(default_factory=list)
    yellow: list[str] = Field(default_factory=list)
    payment: list[str] = Field(default_factory=list)
    red: list[str] = Field(default_factory=list)

    def by_level(self) -> dict[RiskLevel, list[str]]:
        return {
            RiskLevel.GREEN: self.green,
            RiskLevel.YELLOW: self.yellow,
            RiskLevel.PAYMENT: self.payment,
            RiskLevel.RED: self.red,
        }


class SafetyRules(BaseModel):
    kinds: RiskLists = RiskLists()
    keywords: RiskLists = RiskLists()
    flags: RiskLists = RiskLists()
    payment_required_fields: list[str] = Field(default_factory=list)


class PolicyResult(BaseModel):
    decision: Decision
    risk: RiskLevel
    reasons: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)  # payment fields not provided


class SafetyPolicy:
    def __init__(self, rules: SafetyRules) -> None:
        self.rules = rules
        self._kind_risk = {
            kind: level for level, kinds in rules.kinds.by_level().items() for kind in kinds
        }
        self._flag_risk = {
            flag: level for level, flags in rules.flags.by_level().items() for flag in flags
        }
        self._keyword_patterns = [
            (re.compile(rf"(?<!\w){re.escape(kw)}(?!\w)", re.IGNORECASE), kw, level)
            for level, kws in rules.keywords.by_level().items()
            for kw in kws
        ]

    @classmethod
    def from_yaml(cls, path: Path = DEFAULT_RULES_PATH) -> SafetyPolicy:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(SafetyRules.model_validate(data))

    def classify(self, action: Action) -> tuple[RiskLevel, list[str]]:
        """Return the action's risk level and the reasons that set it."""
        risk = self._kind_risk.get(action.kind, RiskLevel.YELLOW)
        known = "" if action.kind in self._kind_risk else " (unknown kind)"
        reasons = [f"kind '{action.kind}' is {risk.name}{known}"]

        for pattern, keyword, level in self._keyword_patterns:
            if level > risk and pattern.search(action.target):
                risk = level
                reasons.append(f"target matches '{keyword}' ({level.name})")

        for flag in action.flags:
            level = self._flag_risk.get(flag, RiskLevel.GREEN)
            if level > risk:
                risk = level
                reasons.append(f"flag '{flag}' ({level.name})")

        return risk, reasons

    def check(self, action: Action) -> PolicyResult:
        risk, reasons = self.classify(action)

        if risk == RiskLevel.GREEN:
            return PolicyResult(decision=Decision.ALLOW, risk=risk, reasons=reasons)
        if risk == RiskLevel.YELLOW:
            return PolicyResult(decision=Decision.CONFIRM, risk=risk, reasons=reasons)
        if risk == RiskLevel.PAYMENT:
            missing = [f for f in self.rules.payment_required_fields if not action.args.get(f)]
            if missing:
                reasons.append(f"payment details missing: {', '.join(missing)}")
                return PolicyResult(
                    decision=Decision.REFUSE, risk=risk, reasons=reasons, missing=missing
                )
            return PolicyResult(decision=Decision.CONFIRM, risk=risk, reasons=reasons)
        return PolicyResult(decision=Decision.HANDOFF, risk=risk, reasons=reasons)
