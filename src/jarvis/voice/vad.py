"""Voice activity detection: Silero VAD (the ONNX copy bundled with openWakeWord, ~0.5 ms/frame)."""

from __future__ import annotations

import warnings
from typing import Protocol

import numpy as np


class SpeechDetector(Protocol):
    def speech_prob(self, frame: np.ndarray) -> float: ...
    def reset(self) -> None: ...


class SileroVAD:
    def __init__(self) -> None:
        from openwakeword.vad import VAD

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self._vad = VAD()

    def speech_prob(self, frame: np.ndarray) -> float:
        """0–1 probability that the 80 ms int16 frame contains speech."""
        return float(self._vad.predict(frame, frame_size=640))

    def reset(self) -> None:
        self._vad.reset_states()
