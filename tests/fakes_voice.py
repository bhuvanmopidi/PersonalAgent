"""Fake TTS engine + audio sink shared by voice tests (no sound card needed)."""

import threading
import time
from collections.abc import Iterable, Iterator

import numpy as np

from jarvis.voice.tts import Audio


class FakeEngine:
    def __init__(self, chunks: int = 2) -> None:
        self.texts: list[str] = []
        self.chunks = chunks

    def synthesize(self, text: str) -> Iterator[Audio]:
        self.texts.append(text)
        for _ in range(self.chunks):
            yield Audio(np.zeros(100, dtype=np.float32), 22050)


class FakeSink:
    """Records what was played; `seconds_per_chunk` simulates playback time."""

    def __init__(self, seconds_per_chunk: float = 0.0, fail: bool = False) -> None:
        self.played: list[int] = []  # chunks played per utterance
        self.stopped_early = 0
        self.seconds_per_chunk, self.fail = seconds_per_chunk, fail

    def play(self, chunks: Iterable[Audio], stop: threading.Event) -> None:
        if self.fail:
            raise OSError("no audio device")
        count = 0
        for _ in chunks:
            deadline = time.monotonic() + self.seconds_per_chunk
            while time.monotonic() < deadline:
                if stop.is_set():
                    self.stopped_early += 1
                    self.played.append(count)
                    return
                time.sleep(0.005)
            count += 1
        self.played.append(count)
