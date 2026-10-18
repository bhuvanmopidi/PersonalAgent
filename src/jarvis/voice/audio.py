"""Audio output: streams chunks to the speakers in small blocks so it can stop instantly."""

from __future__ import annotations

import threading
from collections.abc import Iterable
from typing import Any, Protocol

from jarvis.voice.tts import Audio

BLOCK_SECONDS = 0.05  # how quickly stop() takes effect


class AudioSink(Protocol):
    def play(self, chunks: Iterable[Audio], stop: threading.Event) -> None:
        """Blocking: play until the chunks run out or `stop` is set."""
        ...


class SoundDeviceSink:
    def play(self, chunks: Iterable[Audio], stop: threading.Event) -> None:
        import sounddevice as sd

        stream: Any = None
        try:
            for audio in chunks:
                if stop.is_set():
                    break
                if stream is None:  # one stream per utterance: no gaps or clicks between sentences
                    stream = sd.OutputStream(
                        samplerate=audio.sample_rate, channels=1, dtype="float32"
                    )
                    stream.start()
                block = max(1, int(audio.sample_rate * BLOCK_SECONDS))
                samples = audio.samples.reshape(-1, 1)
                for start in range(0, len(samples), block):
                    if stop.is_set():
                        break
                    stream.write(samples[start : start + block])
        finally:
            if stream is not None:
                if stop.is_set():
                    stream.abort()  # drop buffered audio now
                else:
                    stream.stop()  # let the buffer finish playing
                stream.close()
