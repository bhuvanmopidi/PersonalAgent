"""What the agent's planner returns each step: a short plan of up to 5 actions on the page."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Op = Literal["click", "double_click", "type", "press", "select", "scroll", "find", "navigate",
             "back", "wait", "open_app", "switch", "check_file", "attach", "wait_download",
             "open_file"]  # fmt: skip
RiskHint = Literal["none", "irreversible", "payment", "credentials"]
Status = Literal["continue", "done", "ask", "fail"]

MAX_ACTIONS = 5


class AgentAction(BaseModel):
    """One step on the page. `element` is the [number] from the page snapshot."""

    op: Op
    element: int | None = Field(default=None, description="[number] of the element, if any")
    text: str | None = Field(
        default=None,
        description="text to type, option to select, words to find, or an app / window name",
    )
    key: str | None = Field(default=None, description="for press: e.g. Enter, Escape, ArrowDown")
    url: str | None = Field(default=None, description="for navigate: a full https:// URL")
    risk_hint: RiskHint = Field(
        default="none",
        description="irreversible (delete/send/submit/cancel/unsubscribe), payment, credentials",
    )


class AgentStep(BaseModel):
    """status: continue = do these actions, then look again · done = the goal is reached (message =
    what to tell the user) · ask = need the user (message = one short question) · fail = can't do
    it (message = why)."""

    thought: str = Field(description="one short line: what you see and what you'll do")
    actions: list[AgentAction] = Field(default_factory=list, description="0 to 5 actions")
    status: Status
    message: str | None = Field(default=None, description="for done / ask / fail")
