"""Tiny UI sounds, generated in memory (no files)."""

from __future__ import annotations

import numpy as np

from jarvis.telemetry.logging import get_logger

log = get_logger(__name__)

_RATE = 22_050


def _tone(freqs: list[float], ms: int = 70, volume: float = 0.25) -> np.ndarray:
    n = _RATE * ms // 1000
    t = np.arange(n) / _RATE
    parts = [np.sin(2 * np.pi * f * t) for f in freqs]
    wave = np.concatenate(parts) * volume
    fade = min(200, n // 4)  # avoid clicks
    ramp = np.linspace(0, 1, fade)
    for i in range(len(freqs)):
        seg = slice(i * n, (i + 1) * n)
        wave[seg][:fade] *= ramp
        wave[seg][-fade:] *= ramp[::-1]
    return wave.astype(np.float32)


WAKE = _tone([660, 990])  # rising "ba-ding": I'm listening
DONE = _tone([990, 660])  # falling: stopped listening / nothing heard


def play(sound: np.ndarray) -> None:
    """Fire-and-forget. Never raises — a missing audio device must not break listening."""
    try:
        import sounddevice as sd

        sd.play(sound, _RATE)
    except Exception:
        log.debug("could not play sound", exc_info=True)
