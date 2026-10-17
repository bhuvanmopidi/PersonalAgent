# Architecture

Jarvis is a voice-controlled agent that operates Windows apps and websites the way a person would:
it reads the screen and uses the keyboard and mouse. It never uses third-party app APIs to do tasks.

## The pipeline

```
 🎤 speech
   │
 ① Voice      wake word → VAD → streaming speech-to-text                (Phase 4)
   │
 ② Router     resolve context → pick the fastest tier that can do it    (Phases 2, 14)
   │
 ③ Tiers      1 instant · 2 skill replay · 3 text agent · 4 vision agent (Phases 2, 11, 12, 15)
   │            each produces Actions
   ▼
 ④ ActionRunner — the only path to execution                            ✅ Phase 1
   │   SafetyPolicy.check → confirm / handoff / refuse → Executor.execute
   │   wrapped by KillSwitch (Esc) and timed()
   ▼
 ⑤ Executors  OS / keyboard+mouse / browser (CDP) / Office COM          (Phases 2, 5, 9, 10)
   │
 ⑥ Memory     history, contacts, learned skills (encrypted SQLite)      (Phase 13)
   │
 🔊 TTS speaks progress in parallel                                      (Phase 3)
```

## The 4-tier execution ladder

Every command tries the fastest tier first and only falls down a tier on failure or low confidence.

| Tier | How | Target latency |
|---|---|---|
| 1. Instant intents | Regex/classifier match → one direct OS action | ~0.3 s |
| 2. Learned skills | Replay the action sequence of a past success | ~1–2 s |
| 3. Structured agent | LLM reads the UI Automation tree or the DOM as text, plans 3–5 actions per call | ~2–5 s |
| 4. Vision fallback | Screenshot + OmniParser numbered boxes + vision LLM | ~5–15 s |

Successful Tier 3/4 runs are saved as skills, so the next run of the same task uses Tier 2.

## Core data model (`src/jarvis/models.py`)

| Model | Meaning |
|---|---|
| `Intent` | What the user wants (`name`, `args`, `confidence`, `source`) |
| `Action` | One concrete step (`kind`, `target`, `args`, `description`, `flags`) |
| `ActionResult` | What happened (`ok`, `error`, `observation`, `duration_ms`) |
| `RiskLevel` | `GREEN < YELLOW < PAYMENT < RED` |

Everything between the router and the executors speaks in these models.

## The execution path (built in Phase 1)

```
Action ─► ActionRunner.run_action()
            ├─ SafetyPolicy.check()      → ALLOW | CONFIRM | HANDOFF | REFUSE
            ├─ Confirmer.confirm/handoff → asks the user when needed
            ├─ Executor.execute()        → only for ALLOW, or CONFIRM answered "yes"
            └─ timed()                   → duration_ms on the result
KillSwitch.guard(task) — Esc cancels the running task at any point
```

Executors are never called directly. This is how the rule "every risky action goes through
the safety policy" is enforced in code. Details: [safety.md](safety.md).

## Cross-cutting pieces

| Piece | File | Purpose |
|---|---|---|
| Settings | `src/jarvis/config.py` + `config/settings.yaml` | Typed settings, with defaults and validation |
| Logging | `src/jarvis/telemetry/logging.py` | One format with ms timestamps; every line is redacted |
| Metrics | `src/jarvis/telemetry/metrics.py` | `timed("stage")` measures latency per stage |

## What exists today

| Area | Status |
|---|---|
| Scaffold, config, logging, metrics | ✅ Phase 0 |
| Models, safety policy, confirmations, kill switch, redaction, ActionRunner | ✅ Phase 1 |
| Router, executors, voice, browser, desktop apps, agents, memory | Planned |
