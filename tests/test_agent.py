from fakes_voice import FakeEngine, FakeSink

from jarvis.agent import Agent
from jarvis.executors.base import ActionRunner
from jarvis.models import Action, ActionResult
from jarvis.router.regex_router import RegexRouter
from jarvis.safety.confirm import ScriptedConfirmer, SpokenConfirmer
from jarvis.safety.killswitch import KillSwitch
from jarvis.safety.policy import SafetyPolicy
from jarvis.voice.speaker import Speaker


class RecordingExecutor:
    def __init__(self, ok: bool = True) -> None:
        self.actions: list[Action] = []
        self.ok = ok

    async def execute(self, action: Action) -> ActionResult:
        self.actions.append(action)
        if not self.ok:
            return ActionResult(ok=False, error="not_found", observation="I couldn't find it")
        return ActionResult(ok=True, observation="Volume 60%")


class NoHook:
    def start(self) -> None: ...
    def stop(self) -> None: ...


def make_agent(ok: bool = True) -> tuple[Agent, RecordingExecutor, FakeEngine]:
    executor, engine = RecordingExecutor(ok), FakeEngine()
    speaker = Speaker(engine, FakeSink())
    confirmer = SpokenConfirmer(ScriptedConfirmer(), speaker)
    runner = ActionRunner(SafetyPolicy.from_yaml(), executor, confirmer)
    kill = KillSwitch("esc", lambda key, cb: NoHook())
    return Agent(RegexRouter.from_yaml(), runner, kill, speaker), executor, engine


async def test_text_to_action_end_to_end() -> None:
    agent, executor, _ = make_agent()
    outcome = await agent.handle("Hey Jarvis, play lofi on YouTube")
    assert outcome.result.ok
    assert executor.actions[0].target.endswith("search_query=lofi")
    assert outcome.route_ms < 50  # Tier-1 routing must be near-instant


async def test_all_tier1_actions_are_green() -> None:
    """Tier-1 commands must never trigger a confirmation (ScriptedConfirmer would say no)."""
    agent, executor, _ = make_agent()
    for text in ["open notepad", "close notepad", "volume 30", "mute", "brightness 40",
                 "open downloads", "find resume", "lock", "google delete my account"]:  # fmt: skip
        assert (await agent.handle(text)).result.ok, text
    assert len(executor.actions) == 9


async def test_unmatched_text_is_spoken() -> None:
    agent, executor, engine = make_agent()
    outcome = await agent.handle("send it to him")
    await agent.speaker.wait()
    assert outcome.result.error == "no_match" and executor.actions == []
    assert engine.texts == ["I can't do that yet."]


async def test_speaks_description_while_acting() -> None:
    agent, _, engine = make_agent()
    await agent.handle("open notepad")
    await agent.speaker.wait()
    assert engine.texts == ["Opening notepad"]


async def test_speaks_result_for_level_intents() -> None:
    agent, _, engine = make_agent()
    await agent.handle("volume up")
    await agent.speaker.wait()
    assert engine.texts == ["Volume 60%"]  # the outcome, not "Volume up"


async def test_failure_is_spoken() -> None:
    agent, _, engine = make_agent(ok=False)
    await agent.handle("open blahblah")
    await agent.speaker.wait()
    assert engine.texts[-1] == "I couldn't find it"
