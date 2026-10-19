"""LLM providers behind one interface: OpenRouter / OpenAI / Claude (cloud) and Ollama (local).

`decide(system, user, schema)` returns the model's answer parsed into `schema` (a Pydantic model):
RouterDecision for understanding a command, AgentStep for the general agent's next actions.
A provider that can't answer raises LLMUnavailable with a plain-words reason, so callers can fail
over or explain.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, cast
from urllib.parse import urlparse

from pydantic import BaseModel, ValidationError

from jarvis.config import LLMSettings
from jarvis.planner.schema import RouterDecision
from jarvis.telemetry.logging import get_logger

if TYPE_CHECKING:
    import anthropic
    import ollama
    import openai
    from openai.types.chat import ChatCompletionSystemMessageParam

log = get_logger(__name__)


WARM_TIMEOUT_S = 120.0  # first load of a local model from disk can take ~40 s


class LLMUnavailable(Exception):
    """The provider couldn't give a decision (offline, no key, not installed, timeout, …).

    sticky: the cause won't fix itself in seconds (no key, not installed, offline), so the router
    skips this provider for a while instead of waiting on it for every command.
    """

    def __init__(self, reason: str, sticky: bool = True) -> None:
        super().__init__(reason)
        self.sticky = sticky


def port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    """Fast "is anything listening?" — Windows takes ~2 s to report a refused connection."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@dataclass
class Usage:
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0


@dataclass
class LLMReply:
    decision: Any  # an instance of the schema that was asked for (RouterDecision by default)
    provider: str
    model: str
    ms: float
    usage: Usage = field(default_factory=Usage)


class LLMProvider(Protocol):
    name: str  # "openrouter" | "openai" | "claude" | "local"
    model: str

    async def decide(
        self,
        system: str,
        user: str,
        schema: type[BaseModel] = RouterDecision,
        max_tokens: int = 512,
    ) -> LLMReply: ...


class ClaudeProvider:
    """Claude over the official Anthropic SDK, with structured output and a cached system prompt."""

    name = "claude"

    def __init__(self, settings: LLMSettings, on_offline: Callable[[], None] | None = None) -> None:
        self.model = settings.router_model
        self.effort = settings.effort
        self.timeout_s = settings.timeout_s
        self._client: anthropic.AsyncAnthropic | None = None
        self.on_offline = on_offline  # callable: connection failed → mark offline for a while

    def _get_client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            import anthropic

            # credentials from ANTHROPIC_API_KEY (.env is loaded at startup)
            self._client = anthropic.AsyncAnthropic(timeout=self.timeout_s, max_retries=1)
        return self._client

    async def decide(
        self,
        system: str,
        user: str,
        schema: type[BaseModel] = RouterDecision,
        max_tokens: int = 512,
    ) -> LLMReply:
        import anthropic

        start = time.perf_counter()
        try:
            client = self._get_client()
            response = await client.messages.parse(
                model=self.model,
                max_tokens=max(max_tokens, 1024),
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                output_format=schema,
                output_config={"effort": self.effort},
            )
        except anthropic.AuthenticationError as exc:
            raise LLMUnavailable(
                "Claude rejected the API key — check ANTHROPIC_API_KEY in .env"
            ) from exc
        except anthropic.PermissionDeniedError as exc:
            raise LLMUnavailable("this API key can't use Claude") from exc
        except anthropic.NotFoundError as exc:
            raise LLMUnavailable(f"model {self.model} not found") from exc
        except anthropic.RateLimitError as exc:
            raise LLMUnavailable("Claude is rate-limiting requests right now") from exc
        except anthropic.APIConnectionError as exc:  # includes timeouts
            if self.on_offline is not None:
                self.on_offline()
            raise LLMUnavailable("can't reach Claude (offline?)") from exc
        except anthropic.APIStatusError as exc:
            if "credit" in str(exc).lower():
                raise LLMUnavailable("there are no Claude credits left") from exc
            raise LLMUnavailable(f"Claude error {exc.status_code}") from exc
        except anthropic.AnthropicError as exc:
            raise LLMUnavailable(f"Claude isn't set up: {exc}") from exc
        except TypeError as exc:  # the SDK found no credentials at all
            if "authentication" not in str(exc):
                raise
            raise LLMUnavailable(
                "there's no Claude API key — add ANTHROPIC_API_KEY to .env"
            ) from exc

        if response.stop_reason in ("refusal", "max_tokens") or response.parsed_output is None:
            raise LLMUnavailable(
                f"Claude gave no usable answer ({response.stop_reason})", sticky=False
            )
        u = response.usage
        usage = Usage(
            input_tokens=u.input_tokens,
            cache_read_tokens=u.cache_read_input_tokens or 0,
            cache_write_tokens=u.cache_creation_input_tokens or 0,
            output_tokens=u.output_tokens,
        )
        ms = (time.perf_counter() - start) * 1000
        return LLMReply(response.parsed_output, self.name, self.model, ms, usage)


class OpenAIProvider:
    """OpenAI over the official SDK, structured output (strict JSON schema from RouterDecision).

    OpenAI caches long, identical prompt prefixes automatically — our catalog is deterministic,
    so repeat calls are billed at the cached-input rate.
    """

    name = "openai"
    label = "OpenAI"
    key_env = "OPENAI_API_KEY"
    base_url: str | None = None  # None = api.openai.com

    def __init__(
        self,
        settings: LLMSettings,
        on_offline: Callable[[], None] | None = None,
        model: str | None = None,
        timeout_s: float | None = None,
    ) -> None:
        self.model = model or self.model_from(settings)
        self.timeout_s = timeout_s or settings.timeout_s
        self._client: openai.AsyncOpenAI | None = None
        self.on_offline = on_offline

    def model_from(self, settings: LLMSettings) -> str:
        return settings.openai_model

    def _get_client(self) -> openai.AsyncOpenAI:
        if self._client is None:
            import openai

            key = os.environ.get(self.key_env)  # from .env, loaded at startup
            if not key:
                raise LLMUnavailable(
                    f"there's no {self.label} API key — add {self.key_env} to .env"
                )
            self._client = openai.AsyncOpenAI(
                api_key=key, base_url=self.base_url, timeout=self.timeout_s, max_retries=1
            )
        return self._client

    async def decide(
        self,
        system: str,
        user: str,
        schema: type[BaseModel] = RouterDecision,
        max_tokens: int = 512,
    ) -> LLMReply:
        import openai
        from pydantic import ValidationError as SchemaError

        start = time.perf_counter()
        label = self.label
        try:
            client = self._get_client()
            completion = await client.chat.completions.parse(
                model=self.model,
                messages=[self.system_message(system), {"role": "user", "content": user}],
                response_format=schema,
                temperature=0,
                max_completion_tokens=max_tokens,
            )
        except openai.AuthenticationError as exc:
            raise LLMUnavailable(
                f"{label} rejected the API key — check {self.key_env} in .env"
            ) from exc
        except openai.PermissionDeniedError as exc:
            raise LLMUnavailable(f"this API key can't use {label}") from exc
        except openai.NotFoundError as exc:
            raise LLMUnavailable(f"model {self.model} isn't available on {label}") from exc
        except openai.RateLimitError as exc:
            raise LLMUnavailable(self.rate_limit_reason(exc)) from exc
        except openai.APITimeoutError as exc:
            raise LLMUnavailable(f"{label} took too long", sticky=False) from exc
        except openai.APIConnectionError as exc:
            if self.on_offline is not None:
                self.on_offline()
            raise LLMUnavailable(f"can't reach {label} (offline?)") from exc
        except openai.APIStatusError as exc:
            if exc.status_code == 402:
                raise LLMUnavailable(f"there are no {label} credits left") from exc
            raise LLMUnavailable(f"{label} error {exc.status_code}", sticky=False) from exc
        except (openai.LengthFinishReasonError, SchemaError, ValueError) as exc:
            # the model's JSON didn't fit our schema (free models do this sometimes)
            raise LLMUnavailable(f"{label} returned something unreadable", sticky=False) from exc
        except openai.OpenAIError as exc:
            raise LLMUnavailable(f"{label} isn't set up: {exc}") from exc

        if not completion.choices:
            raise LLMUnavailable(f"{label} gave an empty answer", sticky=False)
        choice = completion.choices[0]
        parsed = choice.message.parsed
        if parsed is None or choice.message.refusal or choice.finish_reason == "length":
            reason = "refused" if choice.message.refusal else choice.finish_reason
            raise LLMUnavailable(f"{label} gave no usable answer ({reason})", sticky=False)
        u = completion.usage
        cached = (
            (u.prompt_tokens_details.cached_tokens or 0) if u and u.prompt_tokens_details else 0
        )
        usage = Usage(
            input_tokens=(u.prompt_tokens - cached) if u else 0,  # uncached part, like Claude's
            cache_read_tokens=cached,
            output_tokens=u.completion_tokens if u else 0,
        )
        ms = (time.perf_counter() - start) * 1000
        return LLMReply(parsed, self.name, self.model, ms, usage)

    def system_message(self, system: str) -> ChatCompletionSystemMessageParam:
        return {"role": "system", "content": system}

    async def warm(self) -> bool:
        """Open the HTTPS connection (and, where supported, write the prompt cache) before the first
        command: a tiny real request with the same system prompt. Costs ~$0.0002. Never raises."""
        try:
            await self._get_client().chat.completions.create(
                model=self.model,
                messages=[self.system_message(self.warm_system), {"role": "user", "content": "ok"}],
                max_completion_tokens=1,
            )
            return True
        except Exception as exc:
            log.info("%s not warmed: %s", self.label, exc)
            return False

    warm_system = ""  # set by the router: the real system prompt, so the cache is the right one

    def rate_limit_reason(self, exc: Exception) -> str:
        if getattr(exc, "code", None) == "insufficient_quota":
            return f"there are no {self.label} credits left"
        return f"{self.label} is rate-limiting requests right now"


class OpenRouterProvider(OpenAIProvider):
    """OpenRouter: one key, many models (incl. free ones) behind an OpenAI-compatible API.

    Free (`:free`) models: ~20 requests/minute and ~50/day without credits (1000/day with $10+).
    """

    name = "openrouter"
    label = "OpenRouter"
    key_env = "OPENROUTER_API_KEY"
    base_url = "https://openrouter.ai/api/v1"

    def model_from(self, settings: LLMSettings) -> str:
        return settings.openrouter_model

    def system_message(self, system: str) -> ChatCompletionSystemMessageParam:
        """Anthropic models only cache when asked: mark the (identical every call) system prompt.
        OpenRouter passes cache_control through; other providers cache automatically."""
        if not self.model.startswith("anthropic/"):
            return {"role": "system", "content": system}
        block = {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        # cache_control isn't in the OpenAI types — OpenRouter accepts it on content blocks
        return cast("ChatCompletionSystemMessageParam", {"role": "system", "content": [block]})

    def rate_limit_reason(self, exc: Exception) -> str:
        if self.model.endswith(":free"):
            return "OpenRouter's free-model limit is reached (about 50 a day without credits)"
        return "OpenRouter is rate-limiting requests right now"


class OllamaProvider:
    """A local model through Ollama — offline, private, free. JSON constrained to our schema."""

    name = "local"

    def __init__(self, settings: LLMSettings) -> None:
        self.model = settings.local_model
        self.url = settings.ollama_url
        self.timeout_s = settings.timeout_s
        self._client: ollama.AsyncClient | None = None

    def _get_client(self) -> ollama.AsyncClient:
        if self._client is None:
            import ollama

            self._client = ollama.AsyncClient(host=self.url, timeout=self.timeout_s)
        return self._client

    async def decide(
        self,
        system: str,
        user: str,
        schema: type[BaseModel] = RouterDecision,
        max_tokens: int = 512,
    ) -> LLMReply:
        import ollama

        start = time.perf_counter()
        parsed = urlparse(self.url)
        host, port = parsed.hostname or "127.0.0.1", parsed.port or 11434
        if not await asyncio.to_thread(port_open, host, port):
            raise LLMUnavailable("Ollama isn't running — install it or start the Ollama app")
        try:
            response = await self._get_client().chat(
                model=self.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                format=schema.model_json_schema(),
                options={"temperature": 0},
                keep_alive="30m",  # stay loaded on the GPU between commands
            )
        except ollama.ResponseError as exc:
            if exc.status_code == 404:
                raise LLMUnavailable(
                    f"the local model isn't downloaded — run: ollama pull {self.model}"
                ) from exc
            raise LLMUnavailable(f"local model error: {exc.error}") from exc
        except (ConnectionError, OSError) as exc:
            raise LLMUnavailable(
                "Ollama isn't running — install it or start the Ollama app"
            ) from exc
        except Exception as exc:  # httpx connect/timeout errors surface with various types
            kind = type(exc).__name__.lower()
            if "timeout" in kind:
                # usually the model still loading onto the GPU (the very first load took ~40 s):
                # not a reason to stop using it
                raise LLMUnavailable("the local model is still loading", sticky=False) from exc
            if "connect" in kind:
                raise LLMUnavailable("Ollama isn't reachable — is the Ollama app running?") from exc
            raise

        try:
            decision = schema.model_validate_json(response.message.content or "")
        except ValidationError as exc:
            raise LLMUnavailable(
                "the local model returned something unreadable", sticky=False
            ) from exc
        usage = Usage(
            input_tokens=response.prompt_eval_count or 0, output_tokens=response.eval_count or 0
        )
        ms = (time.perf_counter() - start) * 1000
        return LLMReply(decision, self.name, self.model, ms, usage)

    async def warm(self, timeout_s: float = WARM_TIMEOUT_S) -> bool:
        """Load the model onto the GPU (an empty request) so the first command is fast.

        Has its own long timeout: loading from disk can take tens of seconds the first time.
        """
        import ollama

        parsed = urlparse(self.url)
        if not await asyncio.to_thread(
            port_open, parsed.hostname or "127.0.0.1", parsed.port or 11434
        ):
            return False
        try:
            client = ollama.AsyncClient(host=self.url, timeout=timeout_s)
            await client.generate(model=self.model, prompt="", keep_alive="30m")
            return True
        except Exception as exc:
            log.info("local model not warmed: %s", exc)
            return False
