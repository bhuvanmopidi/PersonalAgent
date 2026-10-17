# Development guide

## Setup

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"   # once
uv python install 3.12
uv sync
```

In VS Code, set the interpreter to `.venv\Scripts\python.exe` (Ctrl+Shift+P → *Python: Select Interpreter*).

## Commands

| Command | What it does |
|---|---|
| `uv run jarvis --version` | Print the version |
| `uv run jarvis run` | Start the agent (a stub until the voice loop is added) |
| `uv run jarvis demo-safety` | Run fake actions through the safety layer |
| `uv run pytest` | Run the tests |
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
  safety/          policy, confirm, killswitch, redact, demo
  executors/       base.py (ActionRunner) + concrete executors (Phase 2+)
  telemetry/       logging, metrics
  voice/ router/ planner/ tiers/ apps/ memory/   (filled in later phases)
docs/              this documentation
evals/             eval tasks (Phase 16 runner)
tests/             pytest suite (conftest.py has shared fixtures)
```

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
