"""Phase 7: catalog, LLM routing (failover, validation, offline), agent modes — fake providers."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fakes_voice import FakeEngine, FakeSink

from jarvis.agent import Agent
from jarvis.config import LLMSettings
from jarvis.executors.base import ActionRunner
from jarvis.executors.llm_control import LLM_KINDS, LLMControlExecutor
from jarvis.executors.router import KindRouter
from jarvis.models import Action, ActionResult
from jarvis.planner.catalog import Catalog
from jarvis.planner.connectivity import Connectivity
from jarvis.planner.llm import (
    ClaudeProvider,
    LLMReply,
    LLMUnavailable,
    OllamaProvider,
    OpenAIProvider,
    OpenRouterProvider,
    Usage,
)
from jarvis.planner.router import DOWN_FOR_S, LLMRouter
from jarvis.planner.schema import RouterDecision, Slot
from jarvis.router.log import RouteLog
from jarvis.router.regex_router import RegexRouter
from jarvis.safety.confirm import ScriptedConfirmer
from jarvis.safety.killswitch import KillSwitch
from jarvis.safety.policy import SafetyPolicy
from jarvis.voice.speaker import Speaker


def run(intent: str, **slots: str) -> RouterDecision:
    return RouterDecision(
        kind="run_intent", intent=intent, slots=[Slot(name=k, value=v) for k, v in slots.items()]
    )


def say(kind: str, text: str) -> RouterDecision:
    return RouterDecision(kind=kind, text=text)  # type: ignore[arg-type]


class FakeProvider:
    def __init__(self, name: str, *replies: RouterDecision | Exception, delay: float = 0.0) -> None:
        self.name, self.model = name, f"fake-{name}"
        self.replies = list(replies)
        self.calls: list[str] = []
        self.delay = delay

    async def decide(
        self,
        system: str,
        user: str,
        schema: object = None,
        max_tokens: int = 512,
        image: bytes | None = None,
    ) -> LLMReply:
        self.calls.append(user)
        if self.delay:
            await asyncio.sleep(self.delay)
        reply = self.replies.pop(0) if self.replies else LLMUnavailable("no more replies")
        if isinstance(reply, Exception):
            raise reply
        return LLMReply(reply, self.name, self.model, 5.0, Usage(100, 80, 0, 10))


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(scope="module")
def rules() -> RegexRouter:
    return RegexRouter.from_yaml()


@pytest.fixture(scope="module")
def catalog(rules: RegexRouter) -> Catalog:
    return Catalog(rules.specs.values())


def make_router(
    catalog: Catalog, *providers: FakeProvider, online: bool = True, first: str = "claude"
) -> tuple[LLMRouter, Clock]:
    clock = Clock()
    conn = Connectivity(probe=lambda: online, clock=clock)
    router = LLMRouter(catalog, {p.name: p for p in providers}, conn, first, clock=clock)  # type: ignore[arg-type]
    return router, clock


# ---- catalog -------------------------------------------------------------------------------


def test_catalog_is_deterministic_and_big_enough_to_cache(rules: RegexRouter) -> None:
    a = Catalog(rules.specs.values()).system_prompt()
    b = Catalog(reversed(list(rules.specs.values()))).system_prompt()
    assert a == b  # byte-identical prompt → Claude's prompt cache hits
    assert len(a) > 4 * 512  # comfortably above the 512-token minimum cacheable prefix


def test_catalog_slots(catalog: Catalog) -> None:
    assert catalog.intents["youtube_play"].required == {"query"}
    assert catalog.intents["open_app"].required == {"app"}
    assert catalog.intents["open_app"].defaults == {"prefer": "auto"}
    assert catalog.intents["media_seek_to"].required == frozenset()  # time OR amount+unit
    assert "play_default" not in catalog.intents  # rules-only duplicate of youtube_play
    assert "volume_up" in catalog.render() and '"make it louder"' in catalog.render()


def test_to_intent_valid_and_defaults(catalog: Catalog) -> None:
    intent, problem = catalog.to_intent(run("open_app", app="instagram"))
    assert problem is None and intent is not None
    assert (intent.name, intent.args, intent.source) == (
        "open_app", {"prefer": "auto", "app": "instagram"}, "llm",
    )  # fmt: skip


@pytest.mark.parametrize(
    ("decision", "problem"),
    [
        (run("order_pizza", item="margherita"), "not one of the listed intents"),
        (run("volume_set", level="30"), "no slot(s) ['level']"),
        (run("youtube_play"), "needs slot(s): query"),
    ],
)
def test_to_intent_rejects(catalog: Catalog, decision: RouterDecision, problem: str) -> None:
    intent, why = catalog.to_intent(decision)
    assert intent is None and why is not None and problem in why


# ---- routing -------------------------------------------------------------------------------


async def test_online_asks_chosen_provider_first(catalog: Catalog) -> None:
    claude, local = FakeProvider("claude", run("volume_up")), FakeProvider("local")
    router, _ = make_router(catalog, claude, local)
    routed = await router.route("make it louder bro")
    assert routed.provider == "claude" and routed.intent is not None
    assert routed.intent.name == "volume_up" and local.calls == []


async def test_online_local_when_user_chose_it(catalog: Catalog) -> None:
    claude, local = FakeProvider("claude"), FakeProvider("local", run("mute"))
    router, _ = make_router(catalog, claude, local, first="local")
    assert (await router.route("silence")).provider == "local" and claude.calls == []


async def test_offline_uses_only_the_local_model(catalog: Catalog) -> None:
    claude, local = FakeProvider("claude", run("mute")), FakeProvider("local", run("mute"))
    router, _ = make_router(catalog, claude, local, online=False)
    assert (await router.route("silence")).provider == "local" and claude.calls == []


async def test_offline_without_local_model_explains(catalog: Catalog) -> None:
    router, _ = make_router(catalog, FakeProvider("claude"), online=False)
    routed = await router.route("silence")
    assert routed.decision is None and routed.error is not None and "offline" in routed.error


async def test_failover_and_unavailable_provider_is_skipped_for_a_while(catalog: Catalog) -> None:
    claude = FakeProvider("claude", LLMUnavailable("no key"))
    local = FakeProvider("local", run("mute"), run("unmute"), run("mute"))
    router, clock = make_router(catalog, claude, local)
    assert (await router.route("silence")).provider == "local"
    assert (await router.route("sound on")).provider == "local"
    assert len(claude.calls) == 1  # not retried while marked down
    clock.now += DOWN_FOR_S + 1
    await router.route("silence")
    assert len(claude.calls) == 2  # tried again after the cool-down


async def test_invalid_answer_gets_one_retry_with_the_problem(catalog: Catalog) -> None:
    claude = FakeProvider("claude", run("volume_set", level="30"), run("volume_set", value="30"))
    router, _ = make_router(catalog, claude)
    routed = await router.route("volume thirty")
    assert routed.intent is not None and routed.intent.args == {"value": "30"}
    assert "no slot(s) ['level']" in claude.calls[1]
    assert routed.usage.input_tokens == 200  # both calls counted


async def test_invalid_twice_fails_over_without_marking_down(catalog: Catalog) -> None:
    bad = run("nope")
    claude = FakeProvider("claude", bad, bad, run("mute"))
    local = FakeProvider("local", run("mute"))
    router, _ = make_router(catalog, claude, local)
    assert (await router.route("silence")).provider == "local"
    assert (await router.route("silence")).provider == "claude"  # not marked down


async def test_answer_needs_text(catalog: Catalog) -> None:
    claude = FakeProvider("claude", say("answer", ""), say("answer", "I can open apps."))
    router, _ = make_router(catalog, claude)
    routed = await router.route("what can you do")
    assert routed.decision is not None and routed.decision.text == "I can open apps."


def test_connectivity_caches_and_marks_offline() -> None:
    clock, probes = Clock(), []

    def probe() -> bool:
        probes.append(1)
        return True

    conn = Connectivity(probe=probe, clock=clock)
    assert asyncio.run(conn.online()) and asyncio.run(conn.online())
    assert len(probes) == 1  # cached
    conn.mark_offline()
    assert not asyncio.run(conn.online())
    clock.now += 61
    assert asyncio.run(conn.online()) and len(probes) == 2


# ---- providers (no network) ----------------------------------------------------------------


async def test_claude_without_key_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE",
                "ANTHROPIC_CONFIG_DIR"):  # fmt: skip
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(LLMUnavailable, match="API key"):
        await ClaudeProvider(LLMSettings()).decide("system", "hi")


async def test_ollama_not_running_fails_fast() -> None:
    provider = OllamaProvider(LLMSettings(ollama_url="http://127.0.0.1:9"))  # nothing listens
    loop = asyncio.get_running_loop()
    start = loop.time()
    with pytest.raises(LLMUnavailable, match="Ollama isn't running"):
        await provider.decide("system", "hi")
    assert loop.time() - start < 1.5  # not Windows' ~2 s refused-connection wait


# ---- agent ---------------------------------------------------------------------------------


class RecordingExecutor:
    def __init__(self) -> None:
        self.actions: list[Action] = []

    async def execute(self, action: Action) -> ActionResult:
        self.actions.append(action)
        return ActionResult(ok=True, observation="done")


class NoHook:
    def start(self) -> None: ...
    def stop(self) -> None: ...


def make_agent(
    llm: LLMRouter | None, mode: str = "hybrid", log_path: Path | None = None
) -> tuple[Agent, RecordingExecutor, FakeEngine]:
    executor, engine = RecordingExecutor(), FakeEngine()
    speaker = Speaker(engine, FakeSink())
    routed_exec = KindRouter(executor, [(LLM_KINDS, LLMControlExecutor(llm))])
    runner = ActionRunner(SafetyPolicy.from_yaml(), routed_exec, ScriptedConfirmer())
    kill = KillSwitch("esc", lambda key, cb: NoHook())
    agent = Agent(
        RegexRouter.from_yaml(), runner, kill, speaker, llm=llm, mode=mode,  # type: ignore[arg-type]
        route_log=RouteLog(log_path) if log_path else None,
    )  # fmt: skip
    return agent, executor, engine


async def test_hybrid_rules_first_llm_only_for_misses(catalog: Catalog) -> None:
    claude = FakeProvider("claude", run("volume_up", value="20"))
    router, _ = make_router(catalog, claude)
    agent, executor, _ = make_agent(router)
    assert (await agent.handle("volume 30")).route == "rules" and claude.calls == []
    outcome = await agent.handle("crank it up a bit bro")
    assert outcome.route == "claude" and outcome.intent is not None
    assert outcome.intent.source == "llm"
    assert executor.actions[-1].kind == "set_volume"  # through ActionRunner + policy as usual
    assert executor.actions[-1].args["value"] == "20"


async def test_llm_answer_is_spoken_not_acted(catalog: Catalog) -> None:
    claude = FakeProvider("claude", say("answer", "I can open apps and play videos."))
    agent, executor, engine = make_agent(make_router(catalog, claude)[0])
    outcome = await agent.handle("what all can you do")
    await agent.speaker.wait()
    assert executor.actions == [] and outcome.reply == "I can open apps and play videos."
    assert engine.texts[-1] == "I can open apps and play videos."


async def test_unsupported_and_failure_messages(catalog: Catalog) -> None:
    claude = FakeProvider("claude", say("unsupported", "I can't send messages."))
    agent, _, _ = make_agent(make_router(catalog, claude)[0])
    outcome = await agent.handle("send a whatsapp to mom")
    assert (
        outcome.result.error == "unsupported"
        and "can't send messages" in outcome.result.observation
    )
    failing = FakeProvider("claude", LLMUnavailable("there's no Claude API key"))
    agent, _, _ = make_agent(make_router(catalog, failing)[0])
    outcome = await agent.handle("crank it up bro")
    assert outcome.result.observation == (
        "I didn't understand that, and there's no Claude API key."
    )


async def test_no_llm_keeps_old_behaviour() -> None:
    agent, _, _ = make_agent(None)
    assert (await agent.handle("crank it up bro")).result.observation == "I can't do that yet."


async def test_llm_first_mode_and_rules_backup(catalog: Catalog) -> None:
    claude = FakeProvider("claude", run("volume_set", value="30"), LLMUnavailable("offline"))
    agent, _, _ = make_agent(make_router(catalog, claude)[0], mode="llm_first")
    assert (await agent.handle("volume 30")).route == "claude"  # LLM even for a rule match
    assert (await agent.handle("volume 30")).route == "rules"  # LLM failed → rules backup


async def test_context_from_previous_turn_reaches_the_llm(catalog: Catalog) -> None:
    claude = FakeProvider("claude", run("volume_up"))
    agent, _, _ = make_agent(make_router(catalog, claude)[0])
    await agent.handle("volume 30")
    await agent.handle("a bit more")
    assert '"volume 30" -> volume_set(value=30)' in claude.calls[0]


async def test_slow_llm_says_one_moment(catalog: Catalog) -> None:
    claude = FakeProvider("claude", say("answer", "Paris."), delay=0.8)
    agent, _, engine = make_agent(make_router(catalog, claude)[0])
    await agent.handle("capital of france")
    await agent.speaker.wait()
    assert "One moment." in engine.texts


async def test_switch_provider_by_voice(catalog: Catalog) -> None:
    router, _ = make_router(catalog, FakeProvider("claude"), FakeProvider("local"))
    agent, _, _ = make_agent(router)
    outcome = await agent.handle("use the local model")
    assert outcome.result.ok and router.online_provider == "local"
    assert (
        "online and using the local model"
        in (await agent.handle("are you online")).result.observation
    )
    await agent.handle("switch to claude")
    provider: str = router.online_provider  # (mypy narrowed it to "local" above)
    assert provider == "claude"


async def test_route_log_is_written_and_redacted(catalog: Catalog, tmp_path: Path) -> None:
    claude = FakeProvider("claude", say("unsupported", "I can't read OTPs."))
    log_path = tmp_path / "routes.jsonl"
    agent, _, _ = make_agent(make_router(catalog, claude)[0], log_path=log_path)
    await agent.handle("volume 30")
    await agent.handle("my otp is 482913 type it")
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["route"] == "rules" and rows[0]["intent"] == "volume_set"
    assert rows[1]["route"] == "claude" and rows[1]["kind"] == "unsupported"
    assert "482913" not in log_path.read_text(encoding="utf-8")
    assert rows[1]["tokens"]["cache_read_tokens"] == 80


# ---- OpenAI (Phase 7 follow-up) ---------------------------------------------------------------


async def test_cloud_order_openai_then_claude_then_local(catalog: Catalog) -> None:
    openai_ = FakeProvider("openai", LLMUnavailable("no credits"))
    claude = FakeProvider("claude", LLMUnavailable("no credits"))
    local = FakeProvider("local", run("mute"))
    router, _ = make_router(catalog, openai_, claude, local, first="openai")
    assert await router.order() == ["openai", "claude", "local"]
    routed = await router.route("silence")
    assert routed.provider == "local"
    assert [a.split(":")[0] for a in routed.attempts] == ["openai", "claude"]


async def test_use_openai_by_voice(catalog: Catalog) -> None:
    router, _ = make_router(catalog, FakeProvider("openai"), FakeProvider("claude"), first="claude")
    agent, _, _ = make_agent(router)
    assert (await agent.handle("switch to chatgpt")).result.ok
    assert router.online_provider == "openai"
    status = (await agent.handle("which model are you using")).result.observation
    assert status == "I'm online and using OpenAI."


async def test_openai_without_key_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(LLMUnavailable, match="OPENAI_API_KEY"):
        await OpenAIProvider(LLMSettings()).decide("system", "hi")


def test_router_decision_is_a_valid_openai_strict_schema() -> None:
    from openai.lib._pydantic import to_strict_json_schema

    schema = to_strict_json_schema(RouterDecision)
    assert set(schema["required"]) == {"kind", "intent", "slots", "text", "goal", "where"}
    assert schema["additionalProperties"] is False
    assert '"default"' not in json.dumps(schema)  # strict mode rejects defaults


# ---- slot guards (from the first real benchmark) --------------------------------------------


@pytest.mark.parametrize(
    ("text", "intent", "slots", "problem"),
    [
        ("volume 30", "volume_set", {"value": "bytes"}, "number"),  # qwen's real answer
        ("fast forward by 10 min", "media_forward", {"amount": "10", "unit": "seconds"}, "minutes"),
        ("skip ahead 2 minutes", "media_forward", {"amount": "2", "unit": "years"}, "unit"),
        ("open it", "open_app", {"app": "it"}, "refers to"),
        ("close that", "close_app", {"app": "that"}, "refers to"),
        ("turn off the wifi", "ad_policy", {"mode": "off"}, "must be one of"),
        ("go to 5:30", "media_seek_to", {"time": "five thirty"}, "clock time"),
    ],
)
def test_wrong_slot_values_are_rejected(
    catalog: Catalog, text: str, intent: str, slots: dict[str, str], problem: str
) -> None:
    decision = RouterDecision(
        kind="run_intent", intent=intent, slots=[Slot(name=k, value=v) for k, v in slots.items()]
    )
    got, why = catalog.to_intent(decision, text)
    assert got is None and why is not None and problem in why


@pytest.mark.parametrize(
    ("text", "intent", "slots"),
    [
        ("fast forward ten minutes", "media_forward", {"amount": "10", "unit": "minutes"}),
        ("fast forward", "media_forward", {"amount": "10", "unit": "seconds"}),  # no unit said
        ("speed 1.5x", "media_speed", {"rate": "1.5x"}),
        ("set the volume to half", "volume_set", {"value": "50"}),
        ("open instagram website", "open_app", {"app": "instagram", "prefer": "website"}),
    ],
)
def test_right_slot_values_pass(
    catalog: Catalog, text: str, intent: str, slots: dict[str, str]
) -> None:
    decision = RouterDecision(
        kind="run_intent", intent=intent, slots=[Slot(name=k, value=v) for k, v in slots.items()]
    )
    got, why = catalog.to_intent(decision, text)
    assert why is None and got is not None


async def test_wrong_unit_gets_one_retry_then_runs(catalog: Catalog) -> None:
    wrong = run("media_forward", amount="10", unit="seconds")
    right = run("media_forward", amount="10", unit="minutes")
    provider = FakeProvider("local", wrong, right)
    router, _ = make_router(catalog, provider, online=False)
    routed = await router.route("fast forward by 10 min")
    assert routed.intent is not None and routed.intent.args["unit"] == "minutes"
    assert "minutes" in provider.calls[1]  # the retry explained the problem


# ---- OpenRouter ---------------------------------------------------------------------------------


async def test_openrouter_is_first_by_default(catalog: Catalog) -> None:
    router, _ = make_router(
        catalog, FakeProvider("openrouter"), FakeProvider("openai"), FakeProvider("local"),
        first="openrouter",
    )  # fmt: skip
    assert await router.order() == ["openrouter", "openai", "local"]
    assert LLMSettings().online_provider == "openrouter"
    assert LLMSettings().openrouter_model == "anthropic/claude-haiku-5.5"


async def test_openrouter_without_key_never_falls_back_to_the_openai_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-not-for-openrouter")
    with pytest.raises(LLMUnavailable, match="OPENROUTER_API_KEY"):
        await OpenRouterProvider(LLMSettings()).decide("system", "hi")


def test_openrouter_free_limit_message() -> None:
    free = LLMSettings(openrouter_model="nvidia/nemotron-3-super-120b-a12b:free")
    provider = OpenRouterProvider(free)
    assert "50 a day" in provider.rate_limit_reason(Exception())


async def test_use_the_free_model_by_voice(catalog: Catalog) -> None:
    router, _ = make_router(
        catalog, FakeProvider("openrouter"), FakeProvider("openai"), first="openai"
    )
    agent, _, _ = make_agent(router)
    assert (await agent.handle("use openrouter")).result.ok
    assert router.online_provider == "openrouter"


@pytest.mark.parametrize(
    ("text", "slots", "problem"),
    [
        ("fast forward ten minutes", {}, "set slot unit"),  # the default (10 s) would win
        ("fast forward by 30", {"unit": "seconds"}, "set slot amount"),
    ],
)
def test_said_but_left_out(
    catalog: Catalog, text: str, slots: dict[str, str], problem: str
) -> None:
    decision = RouterDecision(
        kind="run_intent", intent="media_forward",
        slots=[Slot(name=k, value=v) for k, v in slots.items()],
    )  # fmt: skip
    got, why = catalog.to_intent(decision, text)
    assert got is None and why is not None and problem in why


def test_clarify_without_words_gets_a_standard_question() -> None:
    from jarvis.planner.catalog import check

    decision = RouterDecision(kind="clarify")
    assert check(decision) is None and decision.text == "Sorry, what should I do?"
    assert check(RouterDecision(kind="answer")) is not None  # an answer must have words


# ---- speed-up: prompt caching + warm-up -------------------------------------------------------


def test_anthropic_models_on_openrouter_mark_the_prompt_for_caching() -> None:
    haiku = OpenRouterProvider(LLMSettings(openrouter_model="anthropic/claude-haiku-5.5"))
    message = haiku.system_message("catalog")
    block = message["content"][0]  # type: ignore[index]
    assert block["cache_control"] == {"type": "ephemeral"} and block["text"] == "catalog"  # type: ignore[index]
    other = OpenRouterProvider(LLMSettings(openrouter_model="openai/gpt-4o-mini"))
    assert other.system_message("catalog") == {"role": "system", "content": "catalog"}


async def test_warm_up_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert await OpenRouterProvider(LLMSettings()).warm() is False


def test_the_client_is_reused_between_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    provider = OpenRouterProvider(LLMSettings())
    assert provider._get_client() is provider._get_client()  # one connection pool per session


# ---- user testing: rules mustn't act on names they can't resolve ---------------------------


class NoAppLauncher:
    def resolve(self, query: str, prefer: str = "auto") -> None:
        return None


async def test_open_unknown_thing_goes_to_the_llm_not_youtube_home(catalog: Catalog) -> None:
    search = run("youtube_search", query="mr beast channel")
    router, _ = make_router(catalog, FakeProvider("openrouter", search), first="openrouter")
    agent, executor, _ = make_agent(router)
    agent.launcher = NoAppLauncher()  # type: ignore[assignment]
    outcome = await agent.handle("open mr beast channel in youtube")
    assert outcome.route == "openrouter" and outcome.intent is not None
    assert outcome.intent.name == "youtube_search"
    assert "mr+beast+channel" in executor.actions[0].target


async def test_open_known_app_stays_on_the_rules(catalog: Catalog) -> None:
    router, _ = make_router(catalog, FakeProvider("openrouter"), first="openrouter")
    agent, _, _ = make_agent(router)  # no launcher wired → rules are trusted
    assert (await agent.handle("open spotify")).route == "rules"


# ---- Phase 11: "task" → the general browser agent -----------------------------------------


class FakeTaskAgent:
    surfaces = {"browser": object(), "desktop": object()}

    def __init__(self) -> None:
        self.goals: list[str] = []
        self.where: list[str] = []

    async def run(self, goal: str, where: str = "browser") -> object:
        from jarvis.tiers.structured.agent import AgentOutcome

        self.goals.append(goal)
        self.where.append(where)
        return AgentOutcome(True, "Playing I Survived 100 Days", steps=4, actions=5)


def task(goal: str, where: str | None = None) -> RouterDecision:
    return RouterDecision(kind="task", goal=goal, where=where)  # type: ignore[arg-type]


async def test_task_goes_to_the_browser_agent(catalog: Catalog) -> None:
    router, _ = make_router(
        catalog, FakeProvider("openrouter", task("play Dream's newest video")), first="openrouter"
    )
    agent, executor, engine = make_agent(router)
    agent.launcher = NoAppLauncher()  # type: ignore[assignment]  # "latest video…" is no app
    fake = FakeTaskAgent()
    agent.task_agent = fake  # type: ignore[assignment]
    outcome = await agent.handle("open me the latest video of dream on youtube")
    assert outcome.intent is not None and outcome.intent.name == "task"
    assert outcome.result.ok and outcome.reply == "Playing I Survived 100 Days"
    assert fake.goals == ["play Dream's newest video"]
    assert fake.where == ["browser"]  # no `where` from the model → a website task
    assert executor.actions == []  # nothing ran outside the agent
    await agent.speaker.wait()
    assert "Working on it." in engine.texts


async def test_app_task_goes_to_the_desktop_agent(catalog: Catalog) -> None:
    decision = task("play Blinding Lights in the Spotify app", where="desktop")
    router, _ = make_router(catalog, FakeProvider("openrouter", decision), first="openrouter")
    agent, executor, _ = make_agent(router)
    fake = FakeTaskAgent()
    agent.task_agent = fake  # type: ignore[assignment]
    outcome = await agent.handle("play blinding lights on spotify")
    assert outcome.intent is not None and outcome.intent.args["where"] == "desktop"
    assert fake.where == ["desktop"] and executor.actions == []


async def test_task_offline_says_so(catalog: Catalog) -> None:
    router, _ = make_router(catalog, FakeProvider("local", task("x")), online=False)
    agent, _, _ = make_agent(router)
    fake = FakeTaskAgent()
    agent.task_agent = fake  # type: ignore[assignment]
    outcome = await agent.handle("do something on a website")
    assert outcome.result.error == "offline" and fake.goals == []


def test_task_needs_a_goal() -> None:
    from jarvis.planner.catalog import check

    decision = RouterDecision(kind="task", text="book a cab")
    assert check(decision) is None and decision.goal == "book a cab"  # falls back to text
    assert check(RouterDecision(kind="task")) is not None
