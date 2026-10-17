# Phase 1: Core models and the safety layer

**Goal:** define the shared data models and build the gate every action must pass through before it
runs. Reference docs: [../safety.md](../safety.md).

## What was built

| File | Purpose |
|---|---|
| `src/jarvis/models.py` | `Intent`, `Action`, `ActionResult`, `RiskLevel` |
| `config/safety.yaml` | Risk rules: kinds, keywords, flags, required payment fields |
| `src/jarvis/safety/policy.py` | `SafetyPolicy.classify()` / `check()` → `PolicyResult` (decision, risk, reasons, missing) |
| `src/jarvis/safety/confirm.py` | `Confirmer` protocol, `TerminalConfirmer`, `ScriptedConfirmer`, prompt builders |
| `src/jarvis/safety/killswitch.py` | `KillSwitch.guard()` with a pynput global Esc hook |
| `src/jarvis/safety/redact.py` | `redact()`, `luhn_valid()`, `RedactingFilter` |
| `src/jarvis/executors/base.py` | `Executor` protocol, `ActionRunner.run_action()` |
| `src/jarvis/safety/demo.py` | `jarvis demo-safety` |
| `tests/test_models.py`, `test_policy.py`, `test_redact.py`, `test_killswitch.py`, `test_run_action.py`, `conftest.py` | 51 new tests (58 total) |

New dependency: `pynput` (plus `types-pynput` for type checking).

## How it works

```
ActionRunner.run_action(action)
  check = policy.check(action)          # risk = max(kind, target keywords, flags)
  REFUSE  → ActionResult(ok=False, error="refused")
  HANDOFF → confirmer.handoff(msg)      # executor never called
  CONFIRM → confirmer.confirm(prompt)   # "no" → error="user_declined"
  execute → executor.execute(action)    # exceptions → ok=False, error="exception: …"
  duration_ms filled in by timed()
```

The kill switch wraps a whole task. `trigger()` is called from pynput's thread and schedules
`task.cancel()` on the event loop with `call_soon_threadsafe`. `guard()` turns that cancellation into
a `Killed` exception, so it can be told apart from other cancellations.

## Design decisions

| Decision | Why |
|---|---|
| Risk only goes up (max of all signals) | A step can't be made "safe" by calling a risky button a plain `click` |
| Unknown kinds → YELLOW | New code is safe by default |
| Keywords match the `target` label only | Typed or dictated content must not trigger confirmations |
| Incomplete payment → REFUSE without asking | A vague "Pay?" prompt is what a misread page or prompt injection would produce |
| RED never reaches the executor | OTP, PIN and passwords are always done by the user |
| Hook active only during tasks; injected keys ignored | Normal Esc use isn't affected, and the agent can't kill itself |
| Luhn check for cards | Order IDs and other long numbers aren't masked |
| Emails and phones not redacted | The agent needs them |
| pynput over `keyboard` | No admin rights needed on Windows |
| Rules in YAML | Safety can be tuned without code changes |

## How to test

```powershell
uv run pytest
uv run jarvis demo-safety   # answer the prompts; press Esc during the 5-second task
```

## Issues found

- The Aadhaar pattern matched the first 12 digits of a 16-digit non-card number. Fixed with
  lookarounds so it only matches an isolated 12-digit group.

## Limitations / next

- Confirmations are terminal-only for now; voice and the overlay come in Phases 3, 4 and 16.
- "Saved payment methods only" is enforced later, in the Amazon flow (Phase 6).
- Handoff waits for Enter; automatic CAPTCHA-cleared detection comes in Phase 5.
- Phase 2 adds real executors behind a regex router.
