"""Safety policy: decides whether an action may run, needs confirmation, or goes to the user.

Every action passes through SafetyPolicy.check() via ActionRunner — there is no bypass path.
"""

from __future__ import annotations

import fnmatch
import re
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlparse

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


class UrlRule(BaseModel):
    path_keywords: list[str] = Field(default_factory=list)
    hosts: list[str] = Field(default_factory=list)  # matches the host and its subdomains


class UrlRules(BaseModel):
    yellow: UrlRule = UrlRule()
    payment: UrlRule = UrlRule()
    red: UrlRule = UrlRule()

    def by_level(self) -> dict[RiskLevel, UrlRule]:
        return {
            RiskLevel.YELLOW: self.yellow,
            RiskLevel.PAYMENT: self.payment,
            RiskLevel.RED: self.red,
        }


class CaptchaRules(BaseModel):
    iframe_hosts: list[str] = Field(default_factory=list)
    phrases: list[str] = Field(default_factory=list)


class SafetyRules(BaseModel):
    kinds: RiskLists = RiskLists()
    keywords: RiskLists = RiskLists()
    flags: RiskLists = RiskLists()
    keyword_kinds: list[str] = Field(default_factory=list)  # kinds whose target is a UI label
    url_kinds: list[str] = Field(default_factory=list)  # kinds whose target is a URL
    url_rules: UrlRules = UrlRules()
    captcha: CaptchaRules = CaptchaRules()
    payment_required_fields: list[str] = Field(default_factory=list)
    settings_apps: list[str] = Field(default_factory=list)  # value changes there → confirm
    blocked_paths: list[str] = Field(default_factory=list)  # never attached / opened
    program_extensions: list[str] = Field(default_factory=list)  # opening one → confirm
    risky_keys: RiskLists = RiskLists()  # key combos that act at once (send, delete for good)


_KEY_ALIASES = {"control": "ctrl", "escape": "esc", "return": "enter", "del": "delete",
                "windows": "win", "cmd": "win"}  # fmt: skip
_MOD_ORDER = ["ctrl", "alt", "shift", "win"]


def normalize_keys(combo: str) -> str:
    """'Shift + Del' → 'shift+delete', 'Enter+Ctrl' → 'ctrl+enter' (modifiers first, in order)."""
    parts = [_KEY_ALIASES.get(p, p) for p in re.split(r"\s*\+\s*", combo.strip().lower()) if p]
    mods = sorted((p for p in parts if p in _MOD_ORDER), key=_MOD_ORDER.index)
    return "+".join(mods + [p for p in parts if p not in _MOD_ORDER])


def app_of(page_url: str) -> str:
    """'app://systemsettings' → 'systemsettings' (desktop actions); '' for web pages."""
    return (
        page_url.removeprefix("app://").split("#")[0].lower()
        if page_url.startswith("app://")
        else ""
    )


def url_risk(url: str, rules: UrlRules) -> tuple[RiskLevel, str] | None:
    """Highest level whose host or path rule matches the URL, with the matching rule."""
    if not url:
        return None
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host, path = (parsed.hostname or "").lower(), parsed.path.lower()
    for level in (RiskLevel.RED, RiskLevel.PAYMENT, RiskLevel.YELLOW):
        rule = rules.by_level()[level]
        for h in rule.hosts:
            h_host, _, h_path = h.lower().partition("/")
            if (host == h_host or host.endswith("." + h_host)) and path.startswith("/" + h_path):
                return level, f"host {h}"
        for kw in rule.path_keywords:
            if kw.lower() in path:
                return level, f"path {kw}"
    return None


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
        self._key_risk = {
            normalize_keys(k): level for level, keys in rules.risky_keys.by_level().items()
            for k in keys
        }  # fmt: skip
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
        is_known = action.kind in self._kind_risk
        risk = self._kind_risk.get(action.kind, RiskLevel.YELLOW)
        reasons = [f"kind '{action.kind}' is {risk.name}{'' if is_known else ' (unknown kind)'}"]

        # labels of UI elements carry risk ("Delete forever"); user-built targets like URLs don't
        check_keywords = not is_known or action.kind in self.rules.keyword_kinds
        for pattern, keyword, level in self._keyword_patterns:
            if check_keywords and level > risk and pattern.search(action.target):
                risk = level
                reasons.append(f"target matches '{keyword}' ({level.name})")

        for flag in action.flags:
            level = self._flag_risk.get(flag, RiskLevel.GREEN)
            if level > risk:
                risk = level
                reasons.append(f"flag '{flag}' ({level.name})")

        # key combos that act at once in many apps (Ctrl+Enter sends, Shift+Delete skips the bin)
        if action.kind == "press_key" and "key" in action.args:
            level = self._key_risk.get(normalize_keys(str(action.args["key"])), RiskLevel.GREEN)
            if level > risk:
                risk = level
                reasons.append(f"key {action.args['key']} acts at once ({level.name})")

        # opening a program (a downloaded setup.exe …) runs it: only after a yes
        if action.kind == "open_file":
            suffix = Path(str(action.args.get("path") or action.target)).suffix.lower()
            if suffix in self.rules.program_extensions and risk < RiskLevel.YELLOW:
                risk = RiskLevel.YELLOW
                reasons.append(f"opening {suffix} runs a program (YELLOW)")

        # changing a value in Windows settings (a switch, an option) — confirmed, never silent
        app = app_of(action.page_url)
        changes_setting = "value_change" in action.flags and app in self.rules.settings_apps
        if changes_setting and risk < RiskLevel.YELLOW:
            risk = RiskLevel.YELLOW
            reasons.append(f"changes a Windows setting in {app} (YELLOW)")

        # where it happens: the page it acts on, or the URL it opens
        urls = [("page", action.page_url)]
        if action.kind in self.rules.url_kinds:
            urls.append(("url", action.target))
        for what, url in urls:
            match = url_risk(url, self.rules.url_rules)
            if match and match[0] > risk:
                risk = match[0]
                reasons.append(f"{what} matches {match[1]} ({risk.name})")

        return risk, reasons

    def blocked_file(self, action: Action) -> str | None:
        """A secret / key / profile file an attach or open would hand out: the rule it matches."""
        path = str(action.args.get("path") or "")
        if action.kind not in ("attach_file", "open_file") or not path:
            return None
        low = path.lower().replace("/", "\\")
        return next(
            (p for p in self.rules.blocked_paths if fnmatch.fnmatchcase(low, p.lower())), None
        )

    def check(self, action: Action) -> PolicyResult:
        blocked = self.blocked_file(action)
        if blocked is not None:
            why = f"{action.args.get('path')} is a protected file (matches {blocked})"
            return PolicyResult(decision=Decision.REFUSE, risk=RiskLevel.RED, reasons=[why])
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
