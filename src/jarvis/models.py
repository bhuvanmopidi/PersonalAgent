"""Core data models shared by router, planner, tiers and executors."""

from __future__ import annotations

from enum import IntEnum
from typing import Any

from pydantic import BaseModel, Field


class RiskLevel(IntEnum):
    """How dangerous an action is. Ordered: higher value = riskier."""

    GREEN = 0  # auto: open, play, search, read
    YELLOW = 1  # confirm first: send, submit, delete, order
    PAYMENT = 2  # one-tap checkout: confirm full details, user does OTP/PIN
    RED = 3  # hand control to user: passwords, OTPs, logins, CAPTCHAs


class Intent(BaseModel):
    """What the user wants, as understood by the router."""

    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source: str = "regex"  # regex | embedding | classifier | llm


class Action(BaseModel):
    """One concrete step an executor can perform."""

    kind: str  # launch_app, open_url, hotkey, type_text, click, send, pay, ...
    target: str = ""  # app name, URL, or UI element label
    args: dict[str, Any] = Field(default_factory=dict)
    description: str = ""  # human-readable, used in confirmations and logs
    flags: set[str] = Field(default_factory=set)  # e.g. password_field, captcha_detected
    page_url: str = ""  # browser page the action happens on (filled in before the safety check)


class ActionResult(BaseModel):
    """Every executor returns this so the agent can verify what happened."""

    ok: bool
    error: str | None = None
    observation: str = ""
    duration_ms: float = 0.0
    data: dict[str, Any] = Field(default_factory=dict)  # facts for the agent (e.g. attached file)
