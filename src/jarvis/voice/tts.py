"""Text-to-speech engines. Each yields audio chunk by chunk so playback can start early.

Piper  — local neural voice (main). Loaded once, ~0.1 s to first audio.
SAPI   — built-in Windows voice. No download needed; used as a fallback.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np
import numpy.typing as npt

from jarvis.telemetry.logging import get_logger

log = get_logger(__name__)


@dataclass
class Audio:
    """Mono float32 samples in [-1, 1]."""

    samples: npt.NDArray[np.float32]
    sample_rate: int


class TTSEngine(Protocol):
    def synthesize(self, text: str) -> Iterator[Audio]:
        """Blocking; called from a worker thread."""
        ...


class PiperEngine:
    def __init__(self, model_path: Path, speed: float = 1.0) -> None:
        from piper import PiperVoice, SynthesisConfig

        self.voice = PiperVoice.load(model_path)
        self.config = SynthesisConfig(length_scale=1.0 / speed)

    def synthesize(self, text: str) -> Iterator[Audio]:
        for chunk in self.voice.synthesize(text, syn_config=self.config):  # ~one per sentence
            samples = chunk.audio_float_array.astype(np.float32).reshape(-1)
            yield Audio(samples, chunk.sample_rate)


class SapiEngine:
    _FORMAT_22KHZ_16BIT_MONO = 22

    def __init__(self, speed: float = 1.0) -> None:
        self.rate = max(-10, min(10, round((speed - 1.0) * 10)))  # SAPI rate is -10..10

    def synthesize(self, text: str) -> Iterator[Audio]:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()  # COM must be initialised per thread
        try:
            voice = win32com.client.Dispatch("SAPI.SpVoice")
            fmt = win32com.client.Dispatch("SAPI.SpAudioFormat")
            fmt.Type = self._FORMAT_22KHZ_16BIT_MONO
            stream = win32com.client.Dispatch("SAPI.SpMemoryStream")
            stream.Format = fmt
            voice.AudioOutputStream = stream
            voice.Rate = self.rate
            voice.Speak(text)
            data = bytes(stream.GetData())
        finally:
            pythoncom.CoUninitialize()
        pcm = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        yield Audio(pcm, 22050)


class LazyEngine:
    """Loads the real engine on first use (or prewarm), so startup stays instant."""

    def __init__(self, factory: Callable[[], TTSEngine]) -> None:
        self._factory = factory
        self._engine: TTSEngine | None = None
        self._lock = threading.Lock()

    def _get(self) -> TTSEngine:
        with self._lock:
            if self._engine is None:
                self._engine = self._factory()
            return self._engine

    def prewarm(self) -> None:
        """Load the model and run one tiny synthesis (the first inference is the slowest)."""
        for _ in self._get().synthesize("Ready."):
            pass

    def synthesize(self, text: str) -> Iterator[Audio]:
        return self._get().synthesize(text)


def create_engine(engine: str, voice: str, speed: float) -> TTSEngine | None:
    """Build the configured engine; falls back to SAPI if the Piper voice isn't downloaded."""
    if engine == "none":
        return None
    if engine == "piper":
        from jarvis.voice.voices import voice_path

        path = voice_path(voice)
        if path.exists():
            return LazyEngine(lambda: PiperEngine(path, speed))
        log.warning(
            "Piper voice '%s' not downloaded (run: jarvis voices download) — using Windows SAPI",
            voice,
        )
    return LazyEngine(lambda: SapiEngine(speed))
