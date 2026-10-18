"""CLI entry: jarvis run | listen-test | do "…" | repl | say | voices | apps | demo-safety."""

from __future__ import annotations

import argparse
import asyncio
from typing import TYPE_CHECKING

from jarvis import __version__
from jarvis.config import load_settings
from jarvis.telemetry.logging import get_logger, setup_logging

if TYPE_CHECKING:  # heavy modules are imported lazily so `jarvis --version` stays instant
    from jarvis.agent import Outcome

log = get_logger("jarvis")


async def run(verbose: bool, mute: bool) -> None:
    """Voice mode: wake word / push-to-talk → command → action, mic live during tasks."""
    from jarvis.voice.runtime import run_voice

    settings = load_settings()
    setup_logging("INFO" if verbose else "WARNING")
    log.info("Jarvis %s starting (llm=%s)", __version__, settings.llm.provider)
    await run_voice(settings, mute=mute)


async def listen_test(verbose: bool) -> None:
    from jarvis.voice.runtime import listen_test as run_listen_test

    settings = load_settings()
    setup_logging("INFO" if verbose else "WARNING")
    await run_listen_test(settings)


async def demo_safety() -> None:
    from jarvis.safety.demo import run_demo

    settings = load_settings()
    setup_logging(settings.log_level)
    await run_demo(settings)


def _print_outcome(outcome: Outcome) -> None:
    r = outcome.result
    label = outcome.intent.name if outcome.intent else "no match"
    status = "ok" if r.ok else f"FAILED ({r.error})"
    print(f"  [{label}] {status}: {r.observation}")
    print(f"  route {outcome.route_ms:.1f} ms · action {r.duration_ms:.0f} ms")


async def do(text: str, verbose: bool, mute: bool) -> bool:
    from jarvis.agent import Agent

    settings = load_settings()
    setup_logging("INFO" if verbose else "WARNING")
    agent = Agent.create(settings, mute=mute)
    outcome = await agent.handle(text)
    _print_outcome(outcome)
    await agent.speaker.wait()  # let it finish speaking before the process exits
    return outcome.result.ok


async def repl(verbose: bool, mute: bool) -> None:
    from jarvis.agent import Agent

    settings = load_settings()
    setup_logging("INFO" if verbose else "WARNING")
    agent = Agent.create(settings, mute=mute)
    prewarm = asyncio.create_task(agent.prewarm())
    print("Jarvis (typed mode). Type a command, or 'exit'. Esc stops a running task.")
    while True:
        text = (await asyncio.to_thread(input, "> ")).strip()
        if text.lower() in {"exit", "quit", "bye"}:
            agent.speaker.stop()
            break
        if text:
            await prewarm
            _print_outcome(await agent.handle(text))


async def say(text: str, engine: str | None) -> None:
    import time

    from jarvis.voice.audio import SoundDeviceSink
    from jarvis.voice.speaker import Speaker
    from jarvis.voice.tts import LazyEngine, create_engine

    settings = load_settings()
    setup_logging(settings.log_level)
    tts = settings.tts
    tts_engine = create_engine(engine or tts.engine, tts.voice, tts.speed)
    if isinstance(tts_engine, LazyEngine):
        start = time.perf_counter()
        await asyncio.to_thread(tts_engine.prewarm)
        print(f"  model load + warm-up: {(time.perf_counter() - start) * 1000:.0f} ms")
    speaker = Speaker(tts_engine, SoundDeviceSink())
    await speaker.say(text, wait=True)
    if speaker.last_first_audio_ms is not None:
        print(f"  time to first audio: {speaker.last_first_audio_ms:.0f} ms")


def voices(action: str, name: str | None) -> None:
    from jarvis.voice import voices as v

    if action == "list":
        downloaded = set(v.list_downloaded())
        for voice in v.list_available():
            print(f"{'*' if voice in downloaded else ' '} {voice}")
        print("\n* = downloaded")
        return
    voice = name or load_settings().tts.voice
    if v.is_downloaded(voice):
        print(f"{voice} is already downloaded: {v.voice_path(voice)}")
        return
    print(f"Downloading {voice} …")
    print(f"Saved to {v.download(voice)}")


def apps(query: str | None, refresh: bool) -> None:
    from jarvis.tiers.instant.app_index import AppIndex

    index = AppIndex.load(refresh=refresh)
    if query:
        entry = index.find(query)
        print(f"{query!r} → {entry.name} ({entry.app_id})" if entry else f"{query!r} → not found")
    else:
        for e in sorted(index.entries, key=lambda e: e.name.lower()):
            print(e.name)
        print(f"\n{len(index.entries)} apps")


def cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="jarvis", description="Voice PC agent for Windows")
    parser.add_argument("--version", action="version", version=f"jarvis {__version__}")
    sub = parser.add_subparsers(dest="command")
    p_run = sub.add_parser("run", help='voice mode: say "Hey Jarvis, open Spotify"')
    p_run.add_argument("-v", "--verbose", action="store_true", help="show policy/debug logs")
    p_run.add_argument("--mute", action="store_true", help="don't speak")
    p_listen = sub.add_parser(
        "listen-test", help="check the mic: shows what Jarvis hears, does nothing"
    )
    p_listen.add_argument("-v", "--verbose", action="store_true")
    p_do = sub.add_parser("do", help='run one typed command, e.g. jarvis do "open notepad"')
    p_do.add_argument("text", nargs="+")
    p_do.add_argument("-v", "--verbose", action="store_true", help="show policy/debug logs")
    p_do.add_argument("--mute", action="store_true", help="don't speak")
    p_repl = sub.add_parser("repl", help="type commands in a loop")
    p_repl.add_argument("-v", "--verbose", action="store_true")
    p_repl.add_argument("--mute", action="store_true", help="don't speak")
    p_say = sub.add_parser("say", help="speak a sentence (tests text-to-speech)")
    p_say.add_argument("text", nargs="+")
    p_say.add_argument("--engine", choices=["piper", "sapi"])
    p_voices = sub.add_parser("voices", help="list or download Piper voices")
    p_voices.add_argument("action", choices=["list", "download"])
    p_voices.add_argument("name", nargs="?", help="e.g. en_US-lessac-medium (default: settings)")
    p_apps = sub.add_parser("apps", help="list installed apps, or test what a name resolves to")
    p_apps.add_argument("query", nargs="?")
    p_apps.add_argument("--refresh", action="store_true", help="rebuild the app index")
    sub.add_parser("demo-safety", help="run fake actions through the safety layer")
    args = parser.parse_args(argv)

    if args.command in ("run", "listen-test"):
        try:
            if args.command == "run":
                asyncio.run(run(args.verbose, args.mute))
            else:
                asyncio.run(listen_test(args.verbose))
        except KeyboardInterrupt:
            print("\nBye.")
    elif args.command == "do":
        raise SystemExit(0 if asyncio.run(do(" ".join(args.text), args.verbose, args.mute)) else 1)
    elif args.command == "repl":
        asyncio.run(repl(args.verbose, args.mute))
    elif args.command == "say":
        asyncio.run(say(" ".join(args.text), args.engine))
    elif args.command == "voices":
        voices(args.action, args.name)
    elif args.command == "apps":
        apps(args.query, args.refresh)
    elif args.command == "demo-safety":
        asyncio.run(demo_safety())
    else:
        parser.print_help()


if __name__ == "__main__":
    cli()
