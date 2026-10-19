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


# bookkeeping for the executor (which control, which surface) — meaningless when read aloud
_INTERNAL_ARGS = {"element", "surface"}


def build_confirm_prompt(action: Action, check: PolicyResult) -> str:
    if check.risk == RiskLevel.PAYMENT:
        a = action.args
        return (
            f"Pay {a['price']} for {a['item']} (qty {a['quantity']}), "
            f"deliver to {a['address']}, using {a['payment_method']}?"
        )
    summary = action.description or f"{action.kind} {action.target}".strip()
    details = ", ".join(
        f"{k}={v}"
        for k, v in action.args.items()
        if k not in _INTERNAL_ARGS and str(v) not in summary  # said once is enough
    )
    return f"{summary}" + (f" ({details})" if details else "") + "?"


def build_handoff_message(action: Action, check: PolicyResult) -> str:
    # spoken aloud, so plain words only — the technical reasons go to the log
    summary = action.description or f"{action.kind} {action.target}".strip()
    return f"I need you for this step: {summary}. Say done when you've finished."


def can_cancel(confirmer: object) -> bool:
    """Whether a pending question may be abandoned (e.g. the CAPTCHA went away on its own).

    A terminal prompt can't: its input() thread would keep running and swallow the next line typed.
    """
    return bool(getattr(confirmer, "cancellable", True))


class TerminalConfirmer:
    """Asks in the terminal without blocking the event loop."""

    cancellable = False

    async def confirm(self, prompt: str) -> bool:
        answer = await asyncio.to_thread(input, f"{prompt} [y/N] ")
        return answer.strip().lower() in {"y", "yes"}

    async def handoff(self, message: str) -> bool:
        answer = await asyncio.to_thread(
            input, f"{message}\nPress Enter when done, or type 'cancel': "
        )
        return answer.strip().lower() != "cancel"


class Announcer(Protocol):
    async def say(self, text: str, wait: bool = False) -> None: ...


class SpokenConfirmer:
    """Speaks the prompt aloud, then asks the inner confirmer (typed now, voice from Phase 4)."""

    def __init__(self, inner: Confirmer, speaker: Announcer) -> None:
        self.inner, self.speaker = inner, speaker

    @property
    def cancellable(self) -> bool:
        return can_cancel(self.inner)

    async def confirm(self, prompt: str) -> bool:
        await self.speaker.say(prompt)
        return await self.inner.confirm(prompt)

    async def handoff(self, message: str) -> bool:
        await self.speaker.say(message)
        return await self.inner.handoff(message)


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
