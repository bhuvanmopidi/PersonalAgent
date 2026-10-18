"""Build and run voice mode (`jarvis run`) and the mic check (`jarvis listen-test`)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from jarvis.agent import Agent
from jarvis.config import Settings
from jarvis.telemetry.logging import get_logger
from jarvis.tiers.instant.apps import load_aliases
from jarvis.voice.listener import Heard, Listener, ListenerCore, NoSpeech, Partial, Wake
from jarvis.voice.mic import Microphone
from jarvis.voice.session import VoiceConfirmer, VoiceSession
from jarvis.voice.stt import WhisperSTT
from jarvis.voice.vad import SileroVAD
from jarvis.voice.wakeword import OpenWakeWord

log = get_logger(__name__)


def alias_hotwords() -> list[str]:
    """App and site names from aliases.yaml, so Whisper spells "Spotify", "lofi", "GitHub" right."""
    aliases = load_aliases()
    names = [*aliases.apps, *aliases.sites]
    return [n for n in names if len(n) >= 3]


def pretty_combo(combo: str) -> str:
    """'<ctrl>+<alt>+j' → 'Ctrl+Alt+J'."""
    return "+".join(part.strip("<>").capitalize() for part in combo.split("+"))


@contextmanager
def push_to_talk_hotkey(combo: str, on_press: Callable[[], None]) -> Iterator[None]:
    if not combo:
        yield
        return
    from pynput import keyboard

    hotkeys = keyboard.GlobalHotKeys({combo: on_press})
    hotkeys.start()
    try:
        yield
    finally:
        hotkeys.stop()


def build_listener(settings: Settings, stt: WhisperSTT, is_muted: Callable[[], bool]) -> Listener:
    core = ListenerCore(
        OpenWakeWord(settings.listen.wake_word), SileroVAD(), settings.listen, is_muted
    )
    return Listener(Microphone(), core, stt)


async def _prewarm_stt(stt: WhisperSTT) -> float:
    start = time.perf_counter()
    await asyncio.to_thread(stt.prewarm)
    return time.perf_counter() - start


async def run_voice(settings: Settings, mute: bool = False) -> None:
    stt = WhisperSTT(settings.stt, alias_hotwords())
    confirmer = VoiceConfirmer()
    agent = Agent.create(settings, confirmer=confirmer, mute=mute)
    confirmer.speaker = agent.speaker

    print("Loading speech models and the Jarvis Chrome window…")
    stt_s, _ = await asyncio.gather(_prewarm_stt(stt), agent.prewarm(launch_browser=True))
    print(f"  whisper {stt.model_name} on {stt.device} ({stt_s:.1f} s)")
    if agent.browser is not None:
        print(f"  browser {'connected' if agent.browser.connected else 'not available'}")

    listener = build_listener(settings, stt, lambda: agent.speaker.is_speaking)
    listener.partials = settings.listen.partials and stt.device == "cuda"
    session = VoiceSession(agent, listener, confirmer, settings.listen.wake_sound)
    ptt = settings.hotkeys.push_to_talk
    try:
        with push_to_talk_hotkey(ptt, listener.push_to_talk):
            hint = f" or press {pretty_combo(ptt)}" if ptt else ""
            print(
                f'Ready. Say "Hey Jarvis"{hint}, then your command. Esc stops a task; Ctrl+C quits.'
            )
            await agent.speaker.say("Ready.")  # never say the wake word itself — it could wake us
            await session.run()
    finally:
        await agent.close()


async def listen_test(settings: Settings) -> None:
    """Mic → wake word → transcript, printed with timings. Does not act on anything."""
    stt = WhisperSTT(settings.stt, alias_hotwords())
    print("Loading speech models…")
    stt_s = await _prewarm_stt(stt)
    print(f"  whisper {stt.model_name} on {stt.device} ({stt_s:.1f} s)")
    listener = build_listener(settings, stt, lambda: False)
    listener.partials = settings.listen.partials and stt.device == "cuda"
    ptt = settings.hotkeys.push_to_talk
    with push_to_talk_hotkey(ptt, listener.push_to_talk):
        hint = f" (or press {pretty_combo(ptt)})" if ptt else ""
        print(f'Say "Hey Jarvis"{hint} and speak. Nothing will be executed. Ctrl+C quits.')
        async for event in listener.events():
            if isinstance(event, Wake):
                print(f"🎤 woke ({event.trigger}) — listening…")
            elif isinstance(event, Partial):
                print(f"   … {event.text}")
            elif isinstance(event, NoSpeech):
                print("   (no speech)")
            elif isinstance(event, Heard):
                timing = f"audio {event.audio_ms:.0f} ms, stt {event.stt_ms:.0f} ms"
                print(f'   heard: "{event.text}"  ({timing})')
