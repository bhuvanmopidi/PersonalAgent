"""Speech-to-text: faster-whisper, loaded once, GPU if available (CUDA float16) else CPU (int8)."""

from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import numpy as np

from jarvis.config import STTSettings
from jarvis.paths import data_dir
from jarvis.telemetry.logging import get_logger

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

log = get_logger(__name__)


@dataclass(frozen=True)
class Transcript:
    text: str
    ms: float  # time spent transcribing


class Transcriber(Protocol):
    def transcribe(self, audio: np.ndarray) -> Transcript: ...


def models_dir() -> Path:
    path = data_dir() / "models" / "whisper"
    path.mkdir(parents=True, exist_ok=True)
    return path


def add_cuda_dll_dirs() -> None:
    """CTranslate2 needs cuBLAS; the nvidia-cublas-cu12 wheel puts its DLLs in site-packages."""
    nvidia = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    for bin_dir in nvidia.glob("*/bin"):
        os.add_dll_directory(str(bin_dir))
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"


def to_float(audio: np.ndarray) -> np.ndarray:
    """int16 PCM → float32 in [-1, 1], which Whisper expects."""
    if audio.dtype == np.int16:
        return audio.astype(np.float32) / 32768.0
    return audio.astype(np.float32)


class WhisperSTT:
    """Lazy: loads on prewarm() or first use. Thread-safe (one transcription at a time)."""

    def __init__(self, settings: STTSettings, hotwords: list[str] | None = None) -> None:
        self.settings = settings
        self.hotwords = " ".join(dict.fromkeys(settings.hotwords + (hotwords or []))) or None
        self._model: WhisperModel | None = None
        self._lock = threading.Lock()
        self.device = ""  # "cuda" / "cpu" once loaded
        self.model_name = ""

    def prewarm(self) -> None:
        with self._lock:
            self._ensure_loaded()
            # the first CUDA run is ~5× slower than later ones — pay that cost now
            self._run(np.zeros(16_000, dtype=np.float32))

    def transcribe(self, audio: np.ndarray) -> Transcript:
        with self._lock:
            self._ensure_loaded()
            start = time.perf_counter()
            text = self._run(to_float(audio))
            return Transcript(text, (time.perf_counter() - start) * 1000)

    def _run(self, audio: np.ndarray) -> str:
        assert self._model is not None
        segments, _ = self._model.transcribe(
            audio,
            language="en",
            beam_size=1,  # greedy: fastest, and commands are short
            without_timestamps=True,
            condition_on_previous_text=False,
            vad_filter=False,  # our own VAD already cut the audio
            hotwords=self.hotwords,
        )
        return "".join(s.text for s in segments).strip()

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        want = self.settings.device
        if want in ("auto", "cuda"):
            try:
                self._load(self.settings.model, "cuda", "float16")
                return
            except Exception as exc:
                if want == "cuda":
                    raise
                log.warning("CUDA Whisper unavailable (%s) — falling back to CPU", exc)
        self._load(self.settings.cpu_model, "cpu", "int8")

    def _load(self, name: str, device: str, compute_type: str) -> None:
        if device == "cuda":
            add_cuda_dll_dirs()
        from faster_whisper import WhisperModel

        start = time.perf_counter()
        model = WhisperModel(
            name, device=device, compute_type=compute_type, download_root=str(models_dir())
        )
        if device == "cuda":
            # cuBLAS loads lazily on the first run — make a missing DLL fail here, not mid-command
            segments, _ = model.transcribe(np.zeros(1600, dtype=np.float32), language="en")
            list(segments)  # transcribe() is lazy; consuming it runs the model
        self._model, self.device, self.model_name = model, device, name
        log.info("whisper %s on %s loaded in %.1f s", name, device, time.perf_counter() - start)
