"""Real-desktop smoke tests. Not run by default: `uv run pytest -m desktop`."""

import asyncio

import pytest

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
