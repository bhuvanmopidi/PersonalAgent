import asyncio
from collections.abc import AsyncIterator

import pytest
from fakes_voice import FakeEngine, FakeSink

from jarvis.agent import Agent
from jarvis.executors.base import ActionRunner
from jarvis.models import Action, ActionResult
from jarvis.router.regex_router import RegexRouter
from jarvis.safety.confirm import SpokenConfirmer
from jarvis.safety.killswitch import KillSwitch
from jarvis.safety.policy import SafetyPolicy
from jarvis.voice import session as session_mod
from jarvis.voice.listener import Heard, ListenEvent, Mode, Trigger
from jarvis.voice.session import VoiceConfirmer, VoiceSession
from jarvis.voice.speaker import Speaker


class SlowExecutor:
    def __init__(self, seconds: float = 0.0) -> None:
        self.seconds, self.actions = seconds, list[Action]()

    async def execute(self, action: Action) -> ActionResult:
        self.actions.append(action)
        await asyncio.sleep(self.seconds)
        return ActionResult(ok=True, observation="done")


class NoHook:
    def start(self) -> None: ...
    def stop(self) -> None: ...


def heard(text: str, trigger: Trigger = "wake") -> Heard:
    return Heard(text, trigger, audio_ms=1000, stt_ms=10)


class ScriptedListener:
    """Yields events, sleeping between them so background tasks can run."""

    def __init__(self, events: list[ListenEvent], gap_s: float = 0.05) -> None:
        self.script, self.gap_s = events, gap_s
        self.mode: Mode = "wake"
        self.modes: list[Mode] = []

    async def events(self) -> AsyncIterator[ListenEvent]:
        for event in self.script:
            yield event
            await asyncio.sleep(self.gap_s)
            self.modes.append(self.mode)


def make(
    events: list[ListenEvent], seconds: float = 0.0
) -> tuple[VoiceSession, SlowExecutor, list[str], ScriptedListener]:
    executor = SlowExecutor(seconds)
    speaker = Speaker(FakeEngine(), FakeSink())
    confirmer = VoiceConfirmer(speaker)
    runner = ActionRunner(SafetyPolicy.from_yaml(), executor, SpokenConfirmer(confirmer, speaker))
    agent = Agent(
        RegexRouter.from_yaml(), runner, KillSwitch("esc", lambda k, cb: NoHook()), speaker
    )
    listener = ScriptedListener(events)
    out: list[str] = []
    return (
        VoiceSession(agent, listener, confirmer, wake_sound=False, out=out.append),
        executor,
        out,
        listener,
    )


async def test_spoken_command_runs() -> None:
    session, executor, out, _ = make([heard("Volume 30.")])
    await session.run()
    assert executor.actions[0].kind == "set_volume"
    assert any("[volume_set] ok" in line for line in out)


async def test_mic_is_open_while_task_runs_then_back_to_wake() -> None:
    session, _, _, listener = make([heard("volume 30"), heard("x", "open")], seconds=0.2)
    await session.run()
    assert listener.modes[0] == "open"
    await session.wait()
    assert listener.mode == "wake"


async def test_stop_without_wake_word_kills_running_task() -> None:
    session, executor, out, _ = make([heard("open notepad"), heard("Stop.", "open")], seconds=5)
    await asyncio.wait_for(session.run(), 2)  # would take 5 s if "stop" didn't work
    assert any("FAILED (killed)" in line and "voice" in line for line in out)


async def test_new_command_while_busy_is_not_started() -> None:
    session, executor, out, _ = make([heard("open notepad"), heard("open spotify")], seconds=0.2)
    await session.run()
    assert [a.target for a in executor.actions] == ["notepad"]
    assert any("busy" in line for line in out)


async def test_open_mic_chatter_when_idle_is_ignored() -> None:
    session, executor, _, _ = make([heard("open notepad", "open")])
    await session.run()
    assert executor.actions == []


async def test_confirmation_answered_by_voice() -> None:
    confirmer = VoiceConfirmer()
    answer = asyncio.create_task(confirmer.confirm("Send it?"))
    await asyncio.sleep(0)
    assert confirmer.waiting
    confirmer.deliver("Yes, go ahead.")
    assert await answer is True


async def test_unclear_twice_means_no() -> None:
    confirmer = VoiceConfirmer()
    answer = asyncio.create_task(confirmer.confirm("Send it?"))
    for text in ["hmm", "what"]:
        await asyncio.sleep(0)
        confirmer.deliver(text)
    assert await answer is False


async def test_unclear_then_clear() -> None:
    confirmer = VoiceConfirmer()
    answer = asyncio.create_task(confirmer.confirm("Send it?"))
    for text in ["hmm", "no"]:
        await asyncio.sleep(0)
        confirmer.deliver(text)
    assert await answer is False


async def test_silence_means_no(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_mod, "CONFIRM_TIMEOUT_S", 0.05)
    assert await VoiceConfirmer().confirm("Send it?") is False


async def test_handoff_waits_for_done() -> None:
    confirmer = VoiceConfirmer()
    done = asyncio.create_task(confirmer.handoff("Enter the OTP"))
    for text in ["one two three", "done"]:
        await asyncio.sleep(0)
        confirmer.deliver(text)
    assert await done is True


async def test_session_routes_answer_to_pending_confirmation() -> None:
    session, _, _, _ = make([])
    answer = asyncio.create_task(session.confirmer.confirm("Send it?"))
    await asyncio.sleep(0)
    session.on_heard(heard("yes", "open"))
    assert await answer is True
