# Development guide

## Setup

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"   # once
uv python install 3.12
uv sync
uv run jarvis voices download     # Piper voice, ~60 MB (optional: falls back to Windows SAPI)
```

In VS Code, set the interpreter to `.venv\Scripts\python.exe` (Ctrl+Shift+P → *Python: Select Interpreter*).

## Commands

| Command | What it does |
|---|---|
| `uv run jarvis --version` | Print the version |
| `uv run jarvis run` | Start the agent (a stub until the voice loop is added) |
| `uv run jarvis repl [-v] [--mute]` | Type commands in a loop (`-v` shows policy logs) |
| `uv run jarvis do "open notepad" [--mute]` | Run one command |
| `uv run jarvis say "hello" [--engine sapi]` | Speak a sentence; prints load and time-to-first-audio |
| `uv run jarvis voices list` / `download [name]` | Piper voices (default voice from settings) |
| `uv run jarvis apps [name] [--refresh]` | List apps / see what a name resolves to / rebuild the index |
| `uv run jarvis demo-safety` | Run fake actions through the safety layer |
| `uv run pytest` | Run the unit tests (no side effects) |
| `uv run pytest -m desktop` | Real-desktop smoke tests (opens Notepad, changes and restores volume) |
| `uv run ruff check .` / `uv run ruff format .` | Lint / format |
| `uv run mypy src tests` | Strict type check |
| `uv add <pkg>` / `uv add --dev <pkg>` | Add a dependency |

Run pytest, ruff and mypy before handing a phase over.

## Project layout

```
config/            settings.yaml, safety.yaml, intents.yaml, apps/*.yaml
src/jarvis/
  main.py          CLI entry (jarvis …)
  config.py        settings loader
  models.py        Intent, Action, ActionResult, RiskLevel
  agent.py         Agent.handle(text): router → mapper → ActionRunner → executor
  paths.py         %LOCALAPPDATA%\JarvisAgent locations
  safety/          policy, confirm, killswitch, redact, demo
  router/          regex_router.py (Tier-1 intents)
  executors/       base.py (ActionRunner), os_executor.py
  tiers/instant/   app_index, apps, system, files, mapper
  voice/           tts (Piper, SAPI), audio (output), speaker, voices (download)
  telemetry/       logging, metrics
  planner/ tiers/{skills,structured,vision}/ apps/ memory/   (later phases)
docs/              this documentation
evals/             eval tasks (Phase 16 runner)
tests/             pytest suite (conftest.py has shared fixtures)
```

## Adding a Tier-1 command

1. Add an intent to `config/intents.yaml`, above the generic `close_app` and `open_app` entries.
2. If it uses a new action kind, add a handler in `executors/os_executor.py` and list the kind in
   `config/safety.yaml` (it is YELLOW until you do).
3. Add the utterance to `tests/test_regex_router.py` and a task to `evals/tasks.yaml`.

App nicknames and websites go in `config/apps/aliases.yaml`. Add a site to `prefer_web` if it
should open in the browser even when a same-named app is installed.

## Conventions

- Python 3.12, type hints everywhere (mypy strict), Pydantic for data models.
- Async for I/O. Never block the event loop; use `asyncio.to_thread` for blocking calls.
- Executors return `ActionResult` and are only called through `ActionRunner.run_action()`.
- Selectors, shortcuts and rules live in `config/`, not in code.
- Wrap each pipeline stage in `timed("stage")`.
- Every phase adds tests and a doc in `docs/phases/`.

## Workflow

Each phase goes: plan → approval → implement → docs → phase report → review → the user commits.
See [CLAUDE.md §0](../CLAUDE.md#0-workflow-how-we-work-on-this-repo).
