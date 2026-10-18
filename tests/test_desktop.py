"""Real-desktop smoke tests. Not run by default: `uv run pytest -m desktop`."""

import asyncio
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import numpy as np

from jarvis.agent import Agent
from jarvis.config import load_settings
from jarvis.safety.confirm import ScriptedConfirmer
from jarvis.tiers.instant.system import Volume

pytestmark = pytest.mark.desktop


async def test_open_and_close_notepad() -> None:
    agent = Agent.create(load_settings(), ScriptedConfirmer())
    opened = await agent.handle("open notepad")
    assert opened.result.ok, opened.result.observation
    await asyncio.sleep(2)
    closed = await agent.handle("close notepad")
    assert closed.result.ok, closed.result.observation


def test_piper_synthesizes() -> None:
    from jarvis.voice.tts import PiperEngine
    from jarvis.voice.voices import is_downloaded, voice_path

    voice = load_settings().tts.voice
    if not is_downloaded(voice):
        pytest.skip(f"run: jarvis voices download {voice}")
    chunks = list(PiperEngine(voice_path(voice)).synthesize("Opening Spotify. Volume up."))
    assert len(chunks) >= 1 and sum(len(c.samples) for c in chunks) > 10_000
    assert all(c.samples.min() >= -1.0 and c.samples.max() <= 1.0 for c in chunks)


def test_sapi_synthesizes() -> None:
    from jarvis.voice.tts import SapiEngine

    (audio,) = SapiEngine().synthesize("Opening Spotify")
    assert audio.sample_rate == 22050 and len(audio.samples) > 10_000


def _piper_16k(text: str) -> "np.ndarray":
    """Piper speaks `text`; resampled to 16 kHz int16 with 1 s of silence on both sides."""
    import numpy as np

    from jarvis.voice.tts import PiperEngine
    from jarvis.voice.voices import is_downloaded, voice_path

    voice = load_settings().tts.voice
    if not is_downloaded(voice):
        pytest.skip(f"run: jarvis voices download {voice}")
    chunks = list(PiperEngine(voice_path(voice)).synthesize(text))
    audio = np.concatenate([c.samples for c in chunks])
    sr = chunks[0].sample_rate
    x = np.interp(np.arange(0, len(audio), sr / 16_000), np.arange(len(audio)), audio)
    silence = np.zeros(16_000, dtype=np.int16)
    return np.concatenate([silence, (x * 32767).astype(np.int16), silence])


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("Hey Jarvis, open Spotify.", "open spotify"),  # one breath: command right after wake
        ("Hey Jarvis. Play lofi on YouTube.", "play lofi on youtube"),
    ],
)
async def test_voice_pipeline_end_to_end(spoken: str, expected: str) -> None:
    """Real wake word + VAD + Whisper on synthetic speech → the transcript the router sees."""
    from jarvis.router.regex_router import normalize
    from jarvis.voice.listener import Heard, Listener, ListenerCore, Wake
    from jarvis.voice.mic import ArraySource
    from jarvis.voice.runtime import alias_hotwords
    from jarvis.voice.stt import WhisperSTT
    from jarvis.voice.vad import SileroVAD
    from jarvis.voice.wakeword import OpenWakeWord

    settings = load_settings()
    stt = WhisperSTT(settings.stt, alias_hotwords())
    core = ListenerCore(OpenWakeWord(), SileroVAD(), settings.listen)
    listener = Listener(ArraySource(_piper_16k(spoken)), core, stt)
    events = [e async for e in listener.events()]
    assert isinstance(events[0], Wake)
    heard = next(e for e in events if isinstance(e, Heard))
    assert normalize(heard.text) == expected, heard.text


async def test_volume_set_and_restore() -> None:
    volume = Volume()
    original = volume.get()
    agent = Agent.create(load_settings(), ScriptedConfirmer())
    try:
        target = 20 if original != 20 else 25
        assert (await agent.handle(f"volume {target}")).result.ok
        assert volume.get() == target
    finally:
        volume.set(original)
