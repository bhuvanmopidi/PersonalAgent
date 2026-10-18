from jarvis.agent import Agent
from jarvis.executors.base import ActionRunner
from jarvis.models import Action, ActionResult
from jarvis.router.regex_router import RegexRouter
from jarvis.safety.confirm import ScriptedConfirmer
from jarvis.safety.killswitch import KillSwitch
from jarvis.safety.policy import SafetyPolicy


class RecordingExecutor:
    def __init__(self) -> None:
        self.actions: list[Action] = []

    async def execute(self, action: Action) -> ActionResult:
        self.actions.append(action)
        return ActionResult(ok=True, observation="done")


class NoHook:
    def start(self) -> None: ...
    def stop(self) -> None: ...


def make_agent() -> tuple[Agent, RecordingExecutor]:
    executor = RecordingExecutor()
    runner = ActionRunner(SafetyPolicy.from_yaml(), executor, ScriptedConfirmer())
    kill = KillSwitch("esc", lambda key, cb: NoHook())
    return Agent(RegexRouter.from_yaml(), runner, kill), executor


async def test_text_to_action_end_to_end() -> None:
    agent, executor = make_agent()
    outcome = await agent.handle("Hey Jarvis, play lofi on YouTube")
    assert outcome.result.ok
    assert executor.actions[0].target.endswith("search_query=lofi")
    assert outcome.route_ms < 50  # Tier-1 routing must be near-instant


async def test_all_tier1_actions_are_green() -> None:
    """Tier-1 commands must never trigger a confirmation (ScriptedConfirmer would say no)."""
    agent, executor = make_agent()
    for text in ["open notepad", "close notepad", "volume 30", "mute", "brightness 40",
                 "open downloads", "find resume", "lock", "google delete my account"]:  # fmt: skip
        assert (await agent.handle(text)).result.ok, text
    assert len(executor.actions) == 9


async def test_unmatched_text() -> None:
    agent, executor = make_agent()
    outcome = await agent.handle("send it to him")
    assert outcome.result.error == "no_match" and executor.actions == []
