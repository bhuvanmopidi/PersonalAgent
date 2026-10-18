"""Speaker: speaks in the background so actions never wait for speech.

- New speech replaces old speech (never lag behind what's happening).
- Short phrases are cached ("Opening Spotify" plays instantly the second time).
- Everything is redacted before it is spoken.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator

from jarvis.safety.redact import redact
from jarvis.telemetry.logging import get_logger
from jarvis.voice.audio import AudioSink
from jarvis.voice.tts import Audio, LazyEngine, TTSEngine

log = get_logger(__name__)

CACHE_MAX_ITEMS = 64
CACHE_MAX_CHARS = 60  # only cache short, repeatable phrases


class Speaker:
    def __init__(self, engine: TTSEngine | None, sink: AudioSink) -> None:
        self.engine, self.sink = engine, sink
        self._stop = threading.Event()
        self._task: asyncio.Task[None] | None = None
        self._cache: OrderedDict[str, list[Audio]] = OrderedDict()
        self._cache_lock = threading.Lock()
        self.last_first_audio_ms: float | None = None

    @property
    def enabled(self) -> bool:
        return self.engine is not None

    @property
    def is_speaking(self) -> bool:
        """True while speech is being produced or played (the mic uses this to ignore Jarvis)."""
        return self._task is not None and not self._task.done() and not self._stop.is_set()

    async def prewarm(self) -> None:
        if isinstance(self.engine, LazyEngine):
            await asyncio.to_thread(self.engine.prewarm)

    async def say(self, text: str, wait: bool = False) -> None:
        """Start speaking `text` (replacing anything still being said). Returns immediately."""
        text = redact(text).strip()
        if not text or self.engine is None:
            return
        self.stop()
        stop = self._stop = threading.Event()
        self._task = asyncio.create_task(asyncio.to_thread(self._speak, text, stop))
        if wait:
            await self.wait()

    def stop(self) -> None:
        self._stop.set()

    async def wait(self) -> None:
        if self._task is not None:
            await self._task

    def _speak(self, text: str, stop: threading.Event) -> None:
        try:
            with self._cache_lock:
                cached = self._cache.get(text)
                if cached is not None:
                    self._cache.move_to_end(text)
            if cached is not None:
                self.last_first_audio_ms = 0.0
                self.sink.play(cached, stop)
                return

            produced: list[Audio] = []
            self.sink.play(self._synthesize(text, stop, produced), stop)
            if not stop.is_set() and len(text) <= CACHE_MAX_CHARS:
                with self._cache_lock:
                    self._cache[text] = produced
                    if len(self._cache) > CACHE_MAX_ITEMS:
                        self._cache.popitem(last=False)
        except Exception:  # no audio device etc. — speech must never break the agent
            log.warning("TTS failed for %r", text, exc_info=True)

    def _synthesize(self, text: str, stop: threading.Event, out: list[Audio]) -> Iterator[Audio]:
        assert self.engine is not None
        start = time.perf_counter()
        for i, audio in enumerate(self.engine.synthesize(text)):
            if stop.is_set():
                return
            if i == 0:
                self.last_first_audio_ms = (time.perf_counter() - start) * 1000
                log.debug("tts first_audio_ms=%.0f", self.last_first_audio_ms)
            out.append(audio)
            yield audio
