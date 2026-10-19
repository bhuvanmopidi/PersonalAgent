"""Command line: `jarvis --help` lists every command."""

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


def _load_dotenv() -> None:
    """Secrets (ANTHROPIC_API_KEY) from the project's .env — gitignored, never logged."""
    from dotenv import load_dotenv

    from jarvis.config import PROJECT_ROOT

    load_dotenv(PROJECT_ROOT / ".env", override=False)


async def run(verbose: bool, mute: bool) -> None:
    """Voice mode: wake word / push-to-talk → command → action, mic live during tasks."""
    from jarvis.voice.runtime import run_voice

    settings = load_settings()
    setup_logging("INFO" if verbose else "WARNING")
    log.info("Jarvis %s starting (llm online=%s)", __version__, settings.llm.online_provider)
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
    label = outcome.intent.name if outcome.intent else ("reply" if outcome.reply else "no match")
    status = "ok" if r.ok else f"FAILED ({r.error})"
    print(f"  [{label} via {outcome.route}] {status}: {r.observation}")
    print(f"  understand {outcome.route_ms:.1f} ms · action {r.duration_ms:.0f} ms")


async def do(text: str, verbose: bool, mute: bool) -> bool:
    from jarvis.agent import Agent

    settings = load_settings()
    setup_logging("INFO" if verbose else "WARNING")
    agent = Agent.create(settings, mute=mute)
    try:
        outcome = await agent.handle(text)
        _print_outcome(outcome)
        await agent.speaker.wait()  # let it finish speaking before the process exits
    finally:
        await agent.close()
    return outcome.result.ok


async def repl(verbose: bool, mute: bool) -> None:
    from jarvis.agent import Agent

    settings = load_settings()
    setup_logging("INFO" if verbose else "WARNING")
    agent = Agent.create(settings, mute=mute)
    prewarm = asyncio.create_task(agent.prewarm())  # connects to Jarvis Chrome if it's open
    print("Jarvis (typed mode). Type a command, or 'exit'. Esc stops a running task.")
    try:
        while True:
            text = (await asyncio.to_thread(input, "> ")).strip()
            if text.lower() in {"exit", "quit", "bye"}:
                break
            if text:
                await prewarm
                _print_outcome(await agent.handle(text))
    finally:
        await agent.close()


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


def browser(action: str) -> None:
    import json
    import subprocess
    import urllib.request

    from jarvis.executors.browser_session import (
        cdp_ready,
        chrome_args,
        find_chrome,
        jarvis_chrome_processes,
        profile_dir,
    )

    settings = load_settings().browser
    chrome = find_chrome(settings.chrome_path)
    profile = profile_dir(settings)
    running = jarvis_chrome_processes(profile)

    if action == "status":
        port = settings.cdp_port
        print(f"Chrome:        {chrome or 'NOT FOUND (set browser.chrome_path)'}")
        print(f"Jarvis folder: {profile}  (separate from your normal Chrome profiles)")
        print(f"Running:       {'yes' if running else 'no'}")
        if cdp_ready(port):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as r:
                tabs = [t for t in json.load(r) if t.get("type") == "page"]
            print(f"Control port:  {port} — connected, {len(tabs)} tab(s) open")
        elif running:
            print("Control port:  closed — this window was opened by `jarvis browser setup`;")
            print("               close it and Jarvis will reopen it with control on.")
        else:
            print(f"Control port:  {port} — Jarvis starts Chrome on the first browser command")
        return

    # setup: open the Jarvis folder WITHOUT remote control, so sign-in pages behave normally
    if chrome is None:
        print(
            "Google Chrome wasn't found. Install it, or set browser.chrome_path in settings.yaml."
        )
        return
    if running and cdp_ready(settings.cdp_port):
        print("The Jarvis Chrome window is open with remote control on (Google may block sign-in).")
        print("Close it, then run `jarvis browser setup` again.")
        return
    if running:
        print("The Jarvis Chrome window is already open for setup — sign in there, then close it.")
        return
    urls = ["https://accounts.google.com", "https://www.youtube.com"]
    flags = getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(chrome_args(chrome, profile, None, urls), creationflags=flags, close_fds=True)
    print("Opened the Jarvis Chrome window (a separate Chrome data folder:")
    print(f"  {profile})")
    print("1. Sign in to the sites you want Jarvis to use: Google/YouTube, Gmail, Amazon, …")
    print("2. Close the window when you're done. Logins are remembered.")
    print("Your normal Chrome profiles are not touched, and Jarvis can't control them.")


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


async def llm_status() -> None:
    import os

    from jarvis.planner.connectivity import tcp_reachable

    settings = load_settings()
    s = settings.llm
    online = await asyncio.to_thread(tcp_reachable)
    router_key = os.environ.get("OPENROUTER_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    claude_key = os.environ.get("ANTHROPIC_API_KEY", "")
    rows = [
        ("Internet", "online" if online else "OFFLINE"),
        (
            "OpenRouter key",
            f"found ({router_key[:10]}…)"
            if router_key
            else "MISSING: add OPENROUTER_API_KEY to .env",
        ),
        ("OpenRouter model", s.openrouter_model),
        (
            "OpenAI key",
            f"found ({openai_key[:8]}…)" if openai_key else "not set (optional)",
        ),
        ("OpenAI model", s.openai_model),
        ("Claude key", f"found ({claude_key[:10]}…)" if claude_key else "not set (optional)"),
        ("Claude model", s.router_model),
    ]
    try:
        import ollama

        listed = await ollama.AsyncClient(host=s.ollama_url, timeout=3).list()
        names = sorted(m.model or "" for m in listed.models)
        ready = s.local_model in names or f"{s.local_model}:latest" in names
        rows.append(("Ollama", f"running, models: {', '.join(names) or 'none'}"))
        fix = f"NOT PULLED: run `ollama pull {s.local_model}`"
        rows.append(("Local model", f"{s.local_model}: {'ready' if ready else fix}"))
    except Exception as exc:
        rows.append(
            ("Ollama", f"NOT AVAILABLE ({type(exc).__name__}): winget install Ollama.Ollama")
        )
    first = {"openrouter": "OpenRouter", "openai": "OpenAI", "claude": "Claude",
             "local": "the local model"}[s.online_provider]  # fmt: skip
    rows.append(("Routing", f"rules, then {first} when online; the local model when offline"))
    rows.append(("Mode", settings.router.mode))
    for name, value in rows:
        print(f"{name + ':':24}{value}")


async def llm_ask(text: str, provider: str | None, repeat: int = 1) -> None:
    """Show what a model decides for one sentence — nothing is executed."""
    from typing import cast

    from jarvis.agent import build_llm_router
    from jarvis.planner.router import ProviderName
    from jarvis.router.regex_router import RegexRouter

    settings = load_settings()
    setup_logging("WARNING")
    router = build_llm_router(settings, RegexRouter.from_yaml())
    names = [provider] if provider else list(router.providers)
    for name in names:
        single = build_llm_router(settings, RegexRouter.from_yaml())
        single.providers = {name: router.providers[name]}
        single.online_provider = cast(ProviderName, name)
        for _ in range(repeat):  # same connection each time: shows cold vs warm and the cache
            routed = await single.route(text)
            if routed.decision is None:
                print(f"{name:10} ✗ {routed.error}  ({routed.ms:.0f} ms)")
                break
            d = routed.decision
            what = f"{d.intent} {d.slot_dict()}" if d.kind == "run_intent" else repr(d.text)
            u = routed.usage
            tokens = f"in {u.input_tokens} (cached {u.cache_read_tokens}) out {u.output_tokens}"
            print(f"{name:10} {d.kind}: {what}  ({routed.ms:.0f} ms · {tokens})")


async def eval_router(
    providers: str,
    local_models: str,
    limit: int | None,
    per_tag: int | None,
    openrouter_models: str = "",
) -> None:
    from jarvis.planner.evaluate import load_cases, report, run_eval, sample_per_tag, save_report

    settings = load_settings()
    setup_logging("WARNING")
    cases = load_cases()
    if per_tag:
        cases = sample_per_tag(cases, per_tag)
    if limit:
        cases = cases[:limit]
    targets: list[str] = []
    for p in (x.strip() for x in providers.split(",") if x.strip()):
        if p == "local":
            models = [m.strip() for m in local_models.split(",") if m.strip()]
            targets += [f"local:{m}" for m in models or [settings.llm.local_model]]
        elif p == "openrouter" and openrouter_models.strip():
            targets += [
                f"openrouter:{m.strip()}" for m in openrouter_models.split(",") if m.strip()
            ]
        else:
            targets.append(p)
    print(f"{len(cases)} cases · targets: {', '.join(targets)}")
    results = await run_eval(settings, targets, cases)
    text = report(results, cases)
    print()
    print(text)
    print(f"Saved: {save_report(text)}")


def cli(argv: list[str] | None = None) -> None:
    _load_dotenv()
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
    p_browser = sub.add_parser("browser", help="the Jarvis Chrome window: setup (sign in) | status")
    p_browser.add_argument("action", choices=["setup", "status"])
    p_llm = sub.add_parser("llm", help='language model: status | ask "sentence"')
    p_llm.add_argument("action", choices=["status", "ask"])
    p_llm.add_argument("text", nargs="*")
    p_llm.add_argument("--provider", choices=["openrouter", "openai", "claude", "local"])
    p_llm.add_argument("--repeat", type=int, default=1, help="ask N times on one connection")
    p_eval = sub.add_parser("eval-router", help="benchmark rules vs Claude vs local model")
    p_eval.add_argument(
        "--providers", default="rules,local", help="rules,openrouter,openai,claude,local"
    )
    p_eval.add_argument("--local-models", default="", help="e.g. qwen2.5:3b,llama3.2:3b")
    p_eval.add_argument(
        "--openrouter-models", default="", help="e.g. anthropic/claude-haiku-5.5,openai/gpt-4o-mini"
    )
    p_eval.add_argument("--limit", type=int, help="only the first N cases")
    p_eval.add_argument(
        "--per-tag", type=int, help="only N cases from each category (fits free-model daily limits)"
    )
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
    elif args.command == "browser":
        browser(args.action)
    elif args.command == "llm":
        if args.action == "status":
            asyncio.run(llm_status())
        else:
            asyncio.run(llm_ask(" ".join(args.text), args.provider, args.repeat))
    elif args.command == "eval-router":
        asyncio.run(
            eval_router(
                args.providers, args.local_models, args.limit, args.per_tag, args.openrouter_models
            )
        )
    elif args.command == "demo-safety":
        asyncio.run(demo_safety())
    else:
        parser.print_help()


if __name__ == "__main__":
    cli()
