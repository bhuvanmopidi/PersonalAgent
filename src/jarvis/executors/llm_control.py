"""'use openai' / 'use claude' / 'use the local model' / 'which model are you using'.

Through ActionRunner like every other action (both kinds are GREEN)."""

from __future__ import annotations

from typing import cast

from jarvis.models import Action, ActionResult
from jarvis.planner.router import LLMRouter, ProviderName

LLM_KINDS = frozenset({"set_llm_provider", "llm_status"})
_NAMES = {
    "openrouter": "OpenRouter",
    "openai": "OpenAI",
    "claude": "Claude",
    "local": "the local model",
}


class LLMControlExecutor:
    def __init__(self, router: LLMRouter | None) -> None:
        self.router = router

    async def execute(self, action: Action) -> ActionResult:
        if self.router is None:
            return ActionResult(
                ok=False,
                error="llm_off",
                observation="The language model is switched off in settings.",
            )
        if action.kind == "set_llm_provider":
            if action.target not in _NAMES:
                return ActionResult(ok=False, error="unsupported", observation="Unknown model.")
            self.router.online_provider = cast(ProviderName, action.target)
            return ActionResult(
                ok=True, observation=f"Okay, I'll use {_NAMES[action.target]} when I'm online."
            )
        if action.kind == "llm_status":
            if not await self.router.connectivity.online():
                return ActionResult(
                    ok=True, observation="I'm offline, so I'm using the local model."
                )
            name = _NAMES[self.router.online_provider]
            return ActionResult(ok=True, observation=f"I'm online and using {name}.")
        return ActionResult(ok=False, error="unsupported", observation=f"Can't do '{action.kind}'")
