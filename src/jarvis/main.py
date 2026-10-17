"""Entry point: `jarvis --version`, `jarvis run`."""

from __future__ import annotations

import argparse
import asyncio

from jarvis import __version__
from jarvis.config import load_settings
from jarvis.telemetry.logging import get_logger, setup_logging

log = get_logger("jarvis")


async def run() -> None:
    """Main service loop. Voice loop + router are wired in later phases."""
    settings = load_settings()
    setup_logging(settings.log_level)
    log.info("Jarvis %s starting (llm=%s)", __version__, settings.llm.provider)
    log.info("Nothing to run yet — scaffold only (Phase 0).")


async def demo_safety() -> None:
    from jarvis.safety.demo import run_demo

    settings = load_settings()
    setup_logging(settings.log_level)
    await run_demo(settings)


def cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="jarvis", description="Voice PC agent for Windows")
    parser.add_argument("--version", action="version", version=f"jarvis {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="start the agent")
    sub.add_parser("demo-safety", help="run fake actions through the safety layer")
    args = parser.parse_args(argv)

    if args.command == "run":
        asyncio.run(run())
    elif args.command == "demo-safety":
        asyncio.run(demo_safety())
    else:
        parser.print_help()


if __name__ == "__main__":
    cli()
