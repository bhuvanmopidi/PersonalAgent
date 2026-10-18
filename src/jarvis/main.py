"""CLI entry: jarvis do "…" | repl | apps | demo-safety | run."""

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


async def run() -> None:
    """Main service loop. The voice loop is wired in Phase 4; use `jarvis repl` meanwhile."""
    settings = load_settings()
    setup_logging(settings.log_level)
    log.info("Jarvis %s starting (llm=%s)", __version__, settings.llm.provider)
    log.info("Voice loop arrives in Phase 4 — try `jarvis repl` for typed commands.")


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


async def do(text: str, verbose: bool) -> bool:
    from jarvis.agent import Agent

    settings = load_settings()
    setup_logging("INFO" if verbose else "WARNING")
    outcome = await Agent.create(settings).handle(text)
    _print_outcome(outcome)
    return outcome.result.ok


async def repl(verbose: bool) -> None:
    from jarvis.agent import Agent

    settings = load_settings()
    setup_logging("INFO" if verbose else "WARNING")
    agent = Agent.create(settings)
    prewarm = asyncio.create_task(agent.prewarm())
    print("Jarvis (typed mode). Type a command, or 'exit'. Esc stops a running task.")
    while True:
        text = (await asyncio.to_thread(input, "> ")).strip()
        if text.lower() in {"exit", "quit", "bye"}:
            break
        if text:
            await prewarm
            _print_outcome(await agent.handle(text))


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
    sub.add_parser("run", help="start the agent")
    p_do = sub.add_parser("do", help='run one typed command, e.g. jarvis do "open notepad"')
    p_do.add_argument("text", nargs="+")
    p_do.add_argument("-v", "--verbose", action="store_true", help="show policy/debug logs")
    p_repl = sub.add_parser("repl", help="type commands in a loop")
    p_repl.add_argument("-v", "--verbose", action="store_true")
    p_apps = sub.add_parser("apps", help="list installed apps, or test what a name resolves to")
    p_apps.add_argument("query", nargs="?")
    p_apps.add_argument("--refresh", action="store_true", help="rebuild the app index")
    sub.add_parser("demo-safety", help="run fake actions through the safety layer")
    args = parser.parse_args(argv)

    if args.command == "run":
        asyncio.run(run())
    elif args.command == "do":
        raise SystemExit(0 if asyncio.run(do(" ".join(args.text), args.verbose)) else 1)
    elif args.command == "repl":
        asyncio.run(repl(args.verbose))
    elif args.command == "apps":
        apps(args.query, args.refresh)
    elif args.command == "demo-safety":
        asyncio.run(demo_safety())
    else:
        parser.print_help()


if __name__ == "__main__":
    cli()
