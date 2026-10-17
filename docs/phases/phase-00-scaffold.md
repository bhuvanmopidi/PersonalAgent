# Phase 0: Project scaffold

**Goal:** a clean, tested foundation that every later phase builds on. Nothing is automated yet.

## What was built

| File | Purpose |
|---|---|
| `pyproject.toml` | Project metadata, the `jarvis` command, dependencies, ruff/mypy/pytest config |
| `uv.lock`, `.python-version` | Pinned dependency versions; Python 3.12 |
| `.gitignore`, `.env.example` | Keeps the environment, secrets, logs and databases out of git |
| `config/settings.yaml` | Log level, kill hotkey, router thresholds, LLM provider/model |
| `src/jarvis/config.py` | Loads the YAML into typed `Settings` (Pydantic) |
| `src/jarvis/telemetry/logging.py` | Shared log format with ms timestamps |
| `src/jarvis/telemetry/metrics.py` | `timed()` latency stopwatch |
| `src/jarvis/main.py` | CLI: `--version`, `run` |
| `src/jarvis/*/` | Empty packages matching the planned architecture |
| `tests/` | Tests for the config, metrics and CLI |

## How it works

`uv run jarvis run` → `cli()` parses the arguments → `asyncio.run(run())` → `load_settings()` →
`setup_logging()` → log the startup line.

**Settings.** Nested Pydantic models mirror the YAML sections. Missing keys or a missing file fall back
to defaults. Invalid values (such as a threshold outside 0–1) fail at startup.

**Metrics.** Usage:

```python
with timed("router") as t:
    ...
t.ms  # elapsed milliseconds, recorded even if the block raises
```

## Design decisions

- **uv:** fast installs, a lockfile, and it manages the Python version.
- **src layout:** tests run against the installed package, so packaging mistakes show up early.
- **Typed settings:** config typos fail at startup, not mid-task.
- **Minimal dependencies:** heavy libraries are added only in the phase that needs them.
- **`timed()` from day one:** speed is the #1 requirement, so every stage is measurable.

## How to test

```powershell
uv sync
uv run pytest
uv run jarvis --version
```

## Issues found

- mypy didn't recognise `Field(0.85, ...)` as a default. Fixed by writing `Field(default=0.85, ...)`.
