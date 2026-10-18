"""Wake word: openWakeWord's bundled "hey jarvis" model (ONNX, ~2 ms per 80 ms frame on CPU)."""

from __future__ import annotations

import warnings
from typing import Protocol

import numpy as np


class WakeDetector(Protocol):
    def score(self, frame: np.ndarray) -> float: ...
    def reset(self) -> None: ...


class OpenWakeWord:
    def __init__(self, name: str = "hey_jarvis") -> None:
        import openwakeword
        from openwakeword.model import Model

        if name not in openwakeword.models:
            known = ", ".join(sorted(openwakeword.models))
            raise ValueError(f"unknown wake word '{name}' (bundled: {known})")
        with warnings.catch_warnings():  # it asks for CUDA first; CPU is plenty for this model
            warnings.simplefilter("ignore")
            self._model = Model(wakeword_model_paths=[openwakeword.models[name]["model_path"]])

    def score(self, frame: np.ndarray) -> float:
        """0–1 confidence that the wake word just ended. frame: 1280 int16 samples."""
        return float(max(self._model.predict(frame).values()))

    def reset(self) -> None:
        """Clear internal audio history, so one "Hey Jarvis" can't trigger twice."""
        self._model.reset()
