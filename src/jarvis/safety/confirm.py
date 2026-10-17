"""Asking the user: confirmations (yellow/payment) and handoffs (red).

Terminal implementation for now; the voice + overlay version replaces it in later phases.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from jarvis.models import Action, RiskLevel
from jarvis.safety.policy import PolicyResult


class Confirmer(Protocol):
    async def confirm(self, prompt: str) -> bool:
        """Read back the details; return True only on an explicit yes."""
        ...

    async def handoff(self, message: str) -> bool:
        """Ask the user to do it themselves; return True once they're done, False if cancelled."""
        ...


def build_confirm_prompt(action: Action, check: PolicyResult) -> str:
    if check.risk == RiskLevel.PAYMENT:
        a = action.args
        return (
            f"Pay {a['price']} for {a['item']} (qty {a['quantity']}), "
            f"deliver to {a['address']}, using {a['payment_method']}?"
        )
    summary = action.description or f"{action.kind} {action.target}".strip()
    details = ", ".join(f"{k}={v}" for k, v in action.args.items())
    return f"{summary}" + (f" ({details})" if details else "") + "?"


def build_handoff_message(action: Action, check: PolicyResult) -> str:
    summary = action.description or f"{action.kind} {action.target}".strip()
    return f"I need you for this step: {summary}. Reason: {'; '.join(check.reasons)}."


class TerminalConfirmer:
    """Asks in the terminal without blocking the event loop."""

    async def confirm(self, prompt: str) -> bool:
        answer = await asyncio.to_thread(input, f"{prompt} [y/N] ")
        return answer.strip().lower() in {"y", "yes"}

    async def handoff(self, message: str) -> bool:
        answer = await asyncio.to_thread(
            input, f"{message}\nPress Enter when done, or type 'cancel': "
        )
        return answer.strip().lower() != "cancel"


class ScriptedConfirmer:
    """Pre-programmed answers, for tests and evals. Records every prompt it was shown."""

    def __init__(self, confirm_answers: list[bool] | None = None, handoff_done: bool = True):
        self._answers = list(confirm_answers or [])
        self._handoff_done = handoff_done
        self.prompts: list[str] = []

    async def confirm(self, prompt: str) -> bool:
        self.prompts.append(prompt)
        return self._answers.pop(0) if self._answers else False

    async def handoff(self, message: str) -> bool:
        self.prompts.append(message)
        return self._handoff_done
