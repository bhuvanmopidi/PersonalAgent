"""Listening: mic frames → wake word / push-to-talk → record until you stop talking → Whisper.

    IDLE ──"hey jarvis" / hotkey──► RECORDING ──pause (silence_ms)──► transcribe ──► Heard
      ▲   (mode="open": any speech          │──nothing said in time──► NoSpeech
      │    starts recording too)             └──max_utterance_s──────► transcribe ──► Heard
      └────────────────────────────────────────────────────────────────────────────┘

Mode "wake" (default) needs the wake word. Mode "open" is used while a task runs or a confirmation
is pending, so you can say "stop" or "yes" without the wake word. While Jarvis itself is speaking,
open-mode speech is ignored (it would hear itself), but the wake word still works.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Literal

import numpy as np

from jarvis.config import ListenSettings
from jarvis.router.regex_router import normalize
from jarvis.telemetry.logging import get_logger
from jarvis.voice.mic import FRAME_MS, FrameSource
from jarvis.voice.stt import Transcriber
from jarvis.voice.vad import SpeechDetector
from jarvis.voice.wakeword import WakeDetector

log = get_logger(__name__)

Trigger = Literal["wake", "ptt", "open"]
Mode = Literal["wake", "open"]

PREROLL_FRAMES = 3  # VAD notices speech a little late; keep ~240 ms before it in open mode
TAIL_KEEP_FRAMES = 3  # keep ~240 ms of the trailing silence, drop the rest before Whisper
PARTIAL_EVERY_MS = 800
# Whisper's favourite inventions on noise/breath — never a real command
_HALLUCINATIONS = {"", "you", "thank you", "thanks for watching", "bye", "uh", "um", "hmm"}


@dataclass(frozen=True)
class Wake:
    trigger: Trigger


@dataclass(frozen=True)
class NoSpeech:
    trigger: Trigger


@dataclass(frozen=True)
class Recorded:
    audio: np.ndarray  # int16
    trigger: Trigger


@dataclass(frozen=True)
class Heard:
    text: str
    trigger: Trigger
    audio_ms: float
    stt_ms: float


@dataclass(frozen=True)
class Partial:
    text: str


ListenEvent = Wake | NoSpeech | Heard | Partial


class ListenerCore:
    """Pure state machine: feed 80 ms frames, get events. No threads, no I/O — easy to test."""

    def __init__(
        self,
        wake: WakeDetector,
        vad: SpeechDetector,
        settings: ListenSettings,
        is_muted: Callable[[], bool] = lambda: False,
    ) -> None:
        self.wake, self.vad, self.s, self.is_muted = wake, vad, settings, is_muted
        self.mode: Mode = "wake"
        self.recording = False
        self._trigger: Trigger = "wake"
        self._frames: list[np.ndarray] = []
        self._preroll: deque[np.ndarray] = deque(maxlen=PREROLL_FRAMES)
        self._speech_seen = False
        self._silence_ms = 0
        self._ptt = threading.Event()  # set from the hotkey thread

    def push_to_talk(self) -> None:
        """Thread-safe: start recording on the next frame, no wake word needed."""
        self._ptt.set()

    def buffered(self) -> np.ndarray:
        """Audio recorded so far (for live partial transcripts)."""
        return np.concatenate(self._frames) if self._frames else np.zeros(0, np.int16)

    def feed(self, frame: np.ndarray) -> Wake | NoSpeech | Recorded | None:
        if not self.recording:
            return self._idle(frame)
        return self._record(frame)

    def _idle(self, frame: np.ndarray) -> Wake | None:
        if self._ptt.is_set():
            self._ptt.clear()
            self._start("ptt", [])
            return Wake("ptt")
        if self.wake.score(frame) >= self.s.wake_threshold:
            self.wake.reset()
            self._start("wake", [])
            return Wake("wake")
        listening = self.mode == "open" and not self.is_muted()
        if listening and self.vad.speech_prob(frame) >= self.s.vad_threshold:
            self._start("open", [*self._preroll, frame])
            self._speech_seen = True
            return None
        self._preroll.append(frame)
        return None

    def _record(self, frame: np.ndarray) -> NoSpeech | Recorded | None:
        self._ptt.clear()  # already recording — a second press means nothing
        self._frames.append(frame)
        if self.vad.speech_prob(frame) >= self.s.vad_threshold:
            self._speech_seen, self._silence_ms = True, 0
        elif self._speech_seen:
            self._silence_ms += FRAME_MS

        elapsed_ms = len(self._frames) * FRAME_MS
        if self._speech_seen and self._silence_ms >= self.s.silence_ms:
            return self._finish(trim=True)
        if not self._speech_seen and elapsed_ms >= self.s.no_speech_timeout_s * 1000:
            trigger = self._trigger
            self._stop()
            return NoSpeech(trigger)
        if elapsed_ms >= self.s.max_utterance_s * 1000:
            return self._finish(trim=False)
        return None

    def _start(self, trigger: Trigger, frames: list[np.ndarray]) -> None:
        self.recording, self._trigger, self._frames = True, trigger, list(frames)
        self._speech_seen, self._silence_ms = False, 0
        self._preroll.clear()
        self.vad.reset()

    def _finish(self, trim: bool) -> Recorded:
        frames = self._frames
        if trim:
            silent = self._silence_ms // FRAME_MS
            frames = frames[: len(frames) - max(0, silent - TAIL_KEEP_FRAMES)]
        event = Recorded(np.concatenate(frames), self._trigger)
        self._stop()
        return event

    def _stop(self) -> None:
        self.recording, self._frames = False, []
        self._speech_seen, self._silence_ms = False, 0
        self.wake.reset()  # the recorded audio must not count toward the next wake word


def is_hallucination(text: str) -> bool:
    return normalize(text) in _HALLUCINATIONS


class Listener:
    """Async wrapper: pulls mic frames, runs the core, transcribes off the event loop."""

    def __init__(
        self,
        source: FrameSource,
        core: ListenerCore,
        stt: Transcriber,
        partials: bool = False,
    ) -> None:
        self.source, self.core, self.stt, self.partials = source, core, stt, partials
        self._partial_task: asyncio.Task[str] | None = None
        self._partial_at_ms = 0.0

    @property
    def mode(self) -> Mode:
        return self.core.mode

    @mode.setter
    def mode(self, mode: Mode) -> None:
        self.core.mode = mode

    def push_to_talk(self) -> None:
        self.core.push_to_talk()

    async def events(self) -> AsyncIterator[ListenEvent]:
        async for frame in self.source.frames():
            event = self.core.feed(frame)
            if isinstance(event, Recorded):
                heard = await self._transcribe(event)
                if heard is not None:
                    yield heard
            elif event is not None:
                yield event
            partial = self._poll_partial()
            if partial is not None:
                yield partial

    async def _transcribe(self, event: Recorded) -> Heard | None:
        self._cancel_partial()
        result = await asyncio.to_thread(self.stt.transcribe, event.audio)
        audio_ms = len(event.audio) * 1000 / 16_000
        log.info("heard %r (audio %.0f ms, stt %.0f ms)", result.text, audio_ms, result.ms)
        if is_hallucination(result.text):
            return None
        return Heard(result.text, event.trigger, audio_ms, result.ms)

    def _poll_partial(self) -> Partial | None:
        """Every ~0.8 s while recording, transcribe the audio so far (shown live, not acted on)."""
        if not self.partials:
            return None
        task = self._partial_task
        if task is not None and task.done():
            self._partial_task = None
            text = "" if task.cancelled() or task.exception() else task.result()
            return Partial(text) if text and self.core.recording else None
        if task is None and self.core.recording:
            audio = self.core.buffered()
            audio_ms = len(audio) * 1000 / 16_000
            if audio_ms - self._partial_at_ms >= PARTIAL_EVERY_MS:
                self._partial_at_ms = audio_ms
                self._partial_task = asyncio.create_task(self._partial_text(audio))
        elif not self.core.recording:
            self._partial_at_ms = 0.0
        return None

    async def _partial_text(self, audio: np.ndarray) -> str:
        return (await asyncio.to_thread(self.stt.transcribe, audio)).text

    def _cancel_partial(self) -> None:
        if self._partial_task is not None:
            self._partial_task.cancel()
            self._partial_task = None
        self._partial_at_ms = 0.0
