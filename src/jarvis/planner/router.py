"""LLM routing: which model to ask, failover, and validating what it says.

Offline → the local model. Online → the chosen provider (openrouter | openai | claude | local),
then the others.
A provider that isn't set up or reachable is skipped for a while instead of waited on every time.
An answer that names an unknown intent or slot gets one retry with the problem explained.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from jarvis.models import Intent
from jarvis.planner.catalog import Catalog, check
from jarvis.planner.connectivity import Connectivity
from jarvis.planner.llm import LLMProvider, LLMReply, LLMUnavailable, Usage
from jarvis.planner.schema import RouterDecision
from jarvis.telemetry.logging import get_logger

log = get_logger(__name__)

ProviderName = Literal["openrouter", "openai", "claude", "local"]
CLOUD = ("openrouter", "openai", "claude")
DOWN_FOR_S = 30.0  # a provider that's not set up / not reachable is skipped this long


@dataclass
class Routed:
    decision: RouterDecision | None = None
    intent: Intent | None = None  # set for run_intent
    provider: str | None = None
    model: str | None = None
    ms: float = 0.0
    usage: Usage = field(default_factory=Usage)
    error: str | None = None  # every provider failed — why, in plain words
    attempts: list[str] = field(default_factory=list)


def _add(a: Usage, b: Usage) -> Usage:
    return Usage(
        a.input_tokens + b.input_tokens,
        a.cache_read_tokens + b.cache_read_tokens,
        a.cache_write_tokens + b.cache_write_tokens,
        a.output_tokens + b.output_tokens,
    )


def user_message(text: str, context: str) -> str:
    return f"Recent turns:\n{context}\n\nCommand: {text}"


class LLMRouter:
    def __init__(
        self,
        catalog: Catalog,
        providers: dict[str, LLMProvider],
        connectivity: Connectivity,
        online_provider: ProviderName = "openrouter",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.catalog = catalog
        self.providers = providers
        self.connectivity = connectivity
        # changed at runtime by "use claude" / "use the local model"
        self.online_provider: ProviderName = online_provider
        self.system = catalog.system_prompt()
        self._clock = clock
        self._down: dict[str, tuple[float, str]] = {}  # provider → (skip until, why)

    async def order(self) -> list[str]:
        if not await self.connectivity.online():
            return ["local"] if "local" in self.providers else []
        # the chosen one, then any other cloud provider, then the local model
        order = [self.online_provider, *CLOUD, "local"]
        return [p for p in dict.fromkeys(order) if p in self.providers]

    async def route(self, text: str, context: str = "(none)") -> Routed:
        start = time.perf_counter()
        routed = Routed()
        order = await self.order()
        if not order:
            routed.error = "I'm offline and the local model isn't set up"
        for name in order:
            down = self._down.get(name)
            if down and self._clock() < down[0]:
                routed.attempts.append(f"{name}: {down[1]}")  # known to be unavailable: no wait
                continue
            try:
                reply, intent, usage = await self._ask(
                    self.providers[name], user_message(text, context), text
                )
            except LLMUnavailable as exc:
                routed.attempts.append(f"{name}: {exc}")
                log.info("llm %s unavailable: %s", name, exc)
                if exc.sticky:
                    self._down[name] = (self._clock() + DOWN_FOR_S, str(exc))
                continue
            self._down.pop(name, None)
            routed.decision, routed.intent = reply.decision, intent
            routed.provider, routed.model, routed.usage = reply.provider, reply.model, usage
            routed.error = None
            break
        else:
            if routed.attempts:
                routed.error = routed.attempts[-1].split(": ", 1)[1]
        routed.ms = (time.perf_counter() - start) * 1000
        return routed

    async def _ask(
        self, provider: LLMProvider, user: str, text: str
    ) -> tuple[LLMReply, Intent | None, Usage]:
        reply = await provider.decide(self.system, user)
        intent, problem = self._validate(reply.decision, text)
        usage = reply.usage
        if problem:
            log.info("llm %s answer invalid (%s) — retrying once", provider.name, problem)
            retry = f"{user}\n\nYour previous answer was invalid: {problem} Answer again."
            reply = await provider.decide(self.system, retry)
            usage = _add(usage, reply.usage)
            intent, problem = self._validate(reply.decision, text)
            if problem:
                raise LLMUnavailable(
                    f"{provider.name} gave an invalid answer ({problem})", sticky=False
                )
        return reply, intent, usage

    def _validate(self, decision: RouterDecision, text: str) -> tuple[Intent | None, str | None]:
        if decision.kind == "run_intent":
            return self.catalog.to_intent(decision, text)
        return None, check(decision)
