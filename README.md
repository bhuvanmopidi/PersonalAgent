# Jarvis — Voice PC Agent for Windows

Speak a task; Jarvis does it on your PC like a human would — across the browser and desktop apps.
Fast paths first (instant intents → learned skills), a general accessibility-tree LLM agent for new
tasks, and a vision fallback for everything else. Local-first, with human checkpoints for risky actions.

> Status: Phase 0 — project scaffold.

## Setup

```powershell
# 1. Install uv (once)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# 2. Install Python 3.12 + dependencies
uv python install 3.12
uv sync

# 3. Run
uv run jarvis --version
uv run jarvis run
```

## Development

```powershell
uv run pytest          # tests
uv run ruff check .    # lint
uv run mypy src        # type check
```

See [CLAUDE.md](CLAUDE.md) for architecture, safety rules and the phase roadmap.
