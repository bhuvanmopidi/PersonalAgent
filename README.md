# Jarvis — Voice PC Agent for Windows

Speak a task; Jarvis does it on your PC like a human would — across the browser and desktop apps.
Fast paths first (instant intents → learned skills), a general accessibility-tree LLM agent for new
tasks, and a vision fallback for everything else. Local-first, with human checkpoints for risky actions.

> Status: Phase 4 done — fully voice-controlled Tier-1 commands ("Hey Jarvis, open Spotify"),
> spoken replies, voice confirmations and "stop". Next: Phase 5, browser via CDP.
> Docs: [docs/](docs/README.md)

## Setup

```powershell
# 1. Install uv (once)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# 2. Install Python 3.12 + dependencies
uv python install 3.12
uv sync
uv run jarvis voices download      # local Piper voice (~60 MB); optional, falls back to Windows SAPI

# 3. Run
uv run jarvis run                  # voice: "Hey Jarvis, open Spotify" (or Ctrl+Alt+J, then speak)
uv run jarvis listen-test          # mic check: shows what it hears, does nothing
uv run jarvis repl                 # type commands: "open spotify", "volume 30", "find my resume"
uv run jarvis do "play lofi on youtube"
uv run jarvis demo-safety          # see the safety layer in action
```

## Development

```powershell
uv run pytest          # tests
uv run ruff check .    # lint
uv run mypy src tests  # type check
```

See [docs/](docs/README.md) for architecture, the safety layer and per-phase notes, and
[CLAUDE.md](CLAUDE.md) for the full spec and roadmap.
