# Architecture

Jarvis is a voice-controlled agent that operates Windows apps and websites the way a person would:
it reads the screen and uses the keyboard and mouse. It never uses third-party app APIs to do tasks.

## The pipeline

```
 🎤 speech
   │
 ① Voice      wake word → VAD → streaming speech-to-text                (Phase 4)
   │
 ② Router     rules → LLM (Claude / local model) → intent, answer, or a question  (Phases 2, 7, 14)
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

## Tier 1 (built in Phase 2)

```
text → normalize → RegexRouter (config/intents.yaml) → Intent
     → to_action (template) → Action → ActionRunner → OSExecutor
```

`Agent.handle(text)` in `src/jarvis/agent.py` wires these together and runs each command under the
kill switch. `OSExecutor` handles `launch_app`, `close_app`, `open_url`, `set_volume`,
`set_brightness`, `open_folder`, `find_file` and `lock_screen`. Apps come from a cached index of every
Start-menu app and are launched via `shell:AppsFolder`. Details:
[phases/phase-02-instant.md](phases/phase-02-instant.md).

## Voice out (built in Phase 3)

```
Agent ──say(text)──► Speaker (background, newest-wins, phrase cache, redaction)
                        └─ worker thread: TTSEngine.synthesize → AudioSink.play (50 ms blocks)
                           engines: Piper (local neural) · SAPI (built-in fallback)
```

The agent speaks the intent's description while the action runs, the result for level changes, and
any failure. Confirmations are spoken through `SpokenConfirmer`. Details:
[phases/phase-03-tts.md](phases/phase-03-tts.md).

## Voice in (built in Phase 4)

```
Microphone (80 ms frames) ─► ListenerCore: wake word / push-to-talk → record → VAD end-of-speech
                          ─► Listener: Whisper (GPU small.en ≈ 90 ms) → Heard(text)
                          ─► VoiceSession: answer to a pending confirmation? "stop"? → kill switch
                                           otherwise → Agent.handle(text) as a background task
```

While a task runs the mic is in "open" mode — "stop" and "yes/no" need no wake word — except while
Jarvis itself is speaking. Details: [phases/phase-04-voice-in.md](phases/phase-04-voice-in.md).

## Browser (built in Phase 5)

```
Action ─► ActionRunner: annotate (current tab URL → page_url) → policy → KindRouter
              ├─ OS kinds ─► OSExecutor          ("open instagram" → website → BrowserExecutor)
              └─ browser kinds ─► BrowserExecutor ─► BrowserSession ─CDP─► Jarvis Chrome window
                                    ├─ YouTubeFlow (pick, play, ads)     (separate data folder)
                                    └─ CaptchaGate → handoff via ActionRunner (RED) → resume
```

Chrome 136+ won't let any program drive your normal profiles, so Jarvis uses its own Chrome data
folder (sign in once with `jarvis browser setup`). Details: [phases/phase-05-browser.md](phases/phase-05-browser.md).

## Understanding commands (built in Phase 7)

```
text ─► rules (0.1 ms) ─miss─► LLMRouter ─► offline: local model (Ollama) · online: OpenRouter / OpenAI / Claude / local
                                           ─► RouterDecision: run_intent → validated Intent → ActionRunner
                                                              answer / clarify / unsupported → spoken
```

The LLM's menu is generated from `intents.yaml`, so it can only choose things Jarvis can do, and the
safety layer is unchanged. Every decision is logged (redacted) for the Phase 14 classifier. Details
and the benchmark: [phases/phase-07-llm.md](phases/phase-07-llm.md).

## The general browser agent (built in Phase 11)

```
"open me the latest video of dream" → router: task → BrowserAgent (any website, no per-site code)
  SEE   observer: wait for the page to settle → numbered elements (incl. custom controls; what a
        popup covers is hidden and reported) + text
  THINK planner (Haiku via OpenRouter) → ≤5 actions + status
  ACT   each action → ActionRunner → SafetyPolicy (label keywords, URL rules, field flags,
        planner risk hint — raise-only) → BrowserExecutor (click / type / press / select /
        scroll / find (Ctrl+F) / navigate / back / wait)
  CHECK page changed mid-batch → look again · done / ask / fail · loop = same actions on an
        unchanged page · budgets: 20 rounds, 120 s · Esc / "stop" end it
```

Details: [phases/phase-11-agent.md](phases/phase-11-agent.md).

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
| Regex router, Tier-1 OS tools (apps, URLs, volume, brightness, folders, files, lock), typed CLI | ✅ Phase 2 |
| Text-to-speech: Piper + SAPI fallback, speak-while-acting, spoken confirmations | ✅ Phase 3 |
| Voice input: wake word, push-to-talk, VAD, Whisper (GPU), voice confirmations, "stop" | ✅ Phase 4 |
| Browser: Jarvis Chrome over CDP, YouTube play/ads, media + tab control, CAPTCHA handoff, page-aware risk | ✅ Phase 5 |
| LLM layer: rules → OpenRouter (Claude Haiku 5.5) / OpenAI / Claude / local model (Ollama), offline fallback, router benchmark | ✅ Phase 7 |
| General browser agent: any website, See→Think→Act→Check, every step safety-checked, `jarvis eval-agent` | ✅ Phase 11 |
| Desktop agent (11.2), Amazon/Gmail/Spotify/Word as agent tasks, skills, memory | Planned |
