"""Voice mode: the listener feeds the agent; the mic stays live while tasks run.

- "Hey Jarvis …" / push-to-talk → command → Agent.handle() (as a background task)
- while a task runs: "stop" (no wake word needed) → kill switch → task cancelled
- while a confirmation is pending: the next thing you say is the answer ("yes" / "no")
- the wake word also cuts Jarvis off mid-sentence (barge-in)
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Protocol

from jarvis.agent import Agent, Outcome
from jarvis.telemetry.logging import get_logger
from jarvis.voice import sounds
from jarvis.voice.listener import Heard, ListenEvent, Mode, NoSpeech, Partial, Wake
from jarvis.voice.replies import is_stop, parse_handoff, parse_yes_no

log = get_logger(__name__)

CONFIRM_TIMEOUT_S = 20
HANDOFF_TIMEOUT_S = 600  # OTP / login / CAPTCHA can take a while


class Announcer(Protocol):
    async def say(self, text: str, wait: bool = False) -> None: ...


class VoiceConfirmer:
    """A Confirmer that takes the answer from the next utterance.

    The question itself is spoken by SpokenConfirmer (which wraps this); the listener ignores the
    mic while Jarvis is speaking, so the answer is whatever you say after the question.
    Unclear twice, or silence → treated as NO. A missed "yes" costs a retry; a misheard one could
    send an email — so the safe default wins.
    """

    def __init__(self, speaker: Announcer | None = None) -> None:
        self.speaker = speaker
        self._waiting: asyncio.Future[str] | None = None

    @property
    def waiting(self) -> bool:
        return self._waiting is not None and not self._waiting.done()

    def deliver(self, text: str) -> None:
        if self._waiting is not None and not self._waiting.done():
            self._waiting.set_result(text)

    async def confirm(self, prompt: str) -> bool:
        for attempt in range(2):
            if attempt:
                await self._say("Sorry, was that a yes or a no?")
            text = await self._next(CONFIRM_TIMEOUT_S)
            if text is None:
                await self._say("No answer, so I won't do it.")
                return False
            answer = parse_yes_no(text)
            if answer is not None:
                return answer
        await self._say("I'll take that as a no.")
        return False

    async def handoff(self, message: str) -> bool:
        while True:
            text = await self._next(HANDOFF_TIMEOUT_S)
            if text is None:
                return False
            done = parse_handoff(text)
            if done is not None:
                return done
            await self._say("Say done when you've finished, or cancel.")

    async def _next(self, timeout: float) -> str | None:
        self._waiting = asyncio.get_running_loop().create_future()
        try:
            return await asyncio.wait_for(self._waiting, timeout)
        except TimeoutError:
            return None
        finally:
            self._waiting = None

    async def _say(self, text: str) -> None:
        if self.speaker is not None:
            await self.speaker.say(text)


Printer = Callable[[str], None]


class EventSource(Protocol):
    mode: Mode

    def events(self) -> AsyncIterator[ListenEvent]: ...


class VoiceSession:
    def __init__(
        self,
        agent: Agent,
        listener: EventSource,
        confirmer: VoiceConfirmer,
        wake_sound: bool = True,
        out: Printer = print,
    ) -> None:
        self.agent, self.listener, self.confirmer = agent, listener, confirmer
        self.wake_sound, self.out = wake_sound, out
        self._task: asyncio.Task[Outcome] | None = None

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    async def run(self) -> None:
        async for event in self.listener.events():
            if isinstance(event, Wake):
                self.agent.speaker.stop()  # barge-in: stop talking, the user wants something
                if self.wake_sound:
                    sounds.play(sounds.WAKE)
                self.out("🎤 listening…")
            elif isinstance(event, Partial):
                self.out(f"   … {event.text}")
            elif isinstance(event, NoSpeech):
                if self.wake_sound:
                    sounds.play(sounds.DONE)
                self.out("   (didn't hear anything)")
            elif isinstance(event, Heard):
                self.on_heard(event)
        await self.wait()

    def on_heard(self, heard: Heard) -> None:
        text = heard.text
        self.out(f'   heard: "{text}"  (stt {heard.stt_ms:.0f} ms)')
        if self.confirmer.waiting:
            self.confirmer.deliver(text)
            return
        if is_stop(text):
            if self.agent.kill.active:
                self.out("   ⏹ stopping")
                self.agent.kill.trigger("voice")
            self.agent.speaker.stop()
            return
        if self.busy:
            if heard.trigger != "open":  # they used the wake word — tell them why nothing happens
                self.out("   (busy — say 'stop' to cancel the current task)")
            return
        if heard.trigger == "open":
            return  # open-mic speech that wasn't a reply or "stop": not addressed to Jarvis
        self._task = asyncio.create_task(self._handle(text))

    async def _handle(self, text: str) -> Outcome:
        self.listener.mode = "open"  # mic live for "stop" and confirmation answers
        try:
            outcome = await self.agent.handle(text)
        except Exception:
            log.exception("command failed: %r", text)
            raise
        finally:
            self.listener.mode = "wake"
        r = outcome.result
        label = outcome.intent.name if outcome.intent else "no match"
        status = "ok" if r.ok else f"FAILED ({r.error})"
        self.out(f"   [{label}] {status}: {r.observation}")
        return outcome

    async def wait(self) -> None:
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
