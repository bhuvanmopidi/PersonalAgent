"""Microphone: 16 kHz mono int16 frames of 80 ms, delivered to asyncio.

Audio only ever lives in memory — nothing is written to disk or sent anywhere.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, Protocol

import numpy as np

from jarvis.telemetry.logging import get_logger

log = get_logger(__name__)

SAMPLE_RATE = 16_000
FRAME_SAMPLES = 1280  # 80 ms — the frame size openWakeWord expects
FRAME_MS = FRAME_SAMPLES * 1000 // SAMPLE_RATE
_QUEUE_MAX = 64  # ~5 s; if the consumer stalls, drop the oldest audio instead of growing forever


class FrameSource(Protocol):
    def frames(self) -> AsyncIterator[np.ndarray]: ...


class Microphone:
    """Default input device via sounddevice. The callback runs on PortAudio's thread."""

    def __init__(self, device: int | str | None = None) -> None:
        self.device = device

    async def frames(self) -> AsyncIterator[np.ndarray]:
        import sounddevice as sd

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=_QUEUE_MAX)

        def push(frame: np.ndarray) -> None:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(frame)

        def callback(indata: np.ndarray, frames: int, time: Any, status: Any) -> None:
            if status:
                log.debug("mic status: %s", status)
            loop.call_soon_threadsafe(push, indata[:, 0].copy())

        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=FRAME_SAMPLES,
            device=self.device,
            callback=callback,
        )
        with stream:
            log.info("microphone open: %s", sd.query_devices(stream.device)["name"])
            while True:
                yield await queue.get()


class ArraySource:
    """Plays a pre-recorded int16 array as if it were the mic (tests, evals, offline debugging)."""

    def __init__(self, audio: np.ndarray, realtime: bool = False) -> None:
        self.audio, self.realtime = audio, realtime

    async def frames(self) -> AsyncIterator[np.ndarray]:
        for i in range(0, len(self.audio) - FRAME_SAMPLES + 1, FRAME_SAMPLES):
            yield self.audio[i : i + FRAME_SAMPLES]
            await asyncio.sleep(FRAME_MS / 1000 if self.realtime else 0)
