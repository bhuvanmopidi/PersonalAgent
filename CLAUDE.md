# CLAUDE.md — Voice PC Agent ("Jarvis")

Context for Claude Code. Read this fully before writing code.

## 0. Workflow (how we work on this repo)
- Work is split into small **phases** (roadmap below in §10).
- Per phase: Claude proposes an action plan → user decides → Claude implements → Claude updates **documentation** → Claude gives a **phase report** → user reviews the code manually and accepts → **user commits & pushes**.
- **Documentation (every phase, in `docs/`):** add `docs/phases/phase-NN-<name>.md` (goal, what was built, how it works, design decisions, how to run/test, limitations); update `docs/architecture.md` and the relevant reference docs (e.g. `docs/safety.md`) when behaviour changes; update `docs/README.md` index and the README status line.
- **Phase report** covers: what was built (files + purpose) · how it works · why those choices · how to run/test · limitations / next-phase hooks · suggested commit message.
- **Git rule:** Claude never runs `git init/add/commit/push/remote` or creates GitHub repos. Claude only suggests the commands; the user runs them.
- Claude never switches Claude Code modes on its own; the user controls modes.

## 1. What we're building
A **voice-controlled, general-purpose computer-use agent for Windows**. The user speaks naturally; the agent does the task on the PC **like a human would** — seeing the screen and using keyboard/mouse — across the **browser and desktop apps**.

Example commands:
- "Order boAt Airdopes 141 from Amazon" → opens amazon.in, searches, picks best match, **reads back item/price/address/payment method and asks for confirmation**, clicks Pay; user completes OTP / UPI PIN.
- "Play Blinding Lights on Spotify" → opens Spotify desktop app, searches, plays.
- "Play lofi on YouTube" → opens YouTube, plays top result.
- "Mail my resume to Arjun" → opens Gmail in browser, composes, attaches resume, **reads draft back, asks to confirm**, sends.
- "Make me a Word document" → opens Word; user dictates live; text appears as spoken; "save as report" saves it.
- Desktop/system: open apps, find/move files, volume, brightness, settings.
- Multi-app: "Put this PDF's table in Excel and mail it to Arjun".

Goals: (1) strong resume/placement project, (2) ML depth, (3) possible product later (differentiator: Indian-language voice + local-first privacy).

## 2. Hard constraints
- **Platform: Windows** only (for now).
- **No third-party app/web APIs for doing tasks** (no Spotify Web API, no Gmail API, etc.). The agent must operate the real UI like a human. Allowed: local OS automation — UI Automation (pywinauto), keyboard/mouse (pyautogui), Office COM (win32com), URL navigation / URL schemes (e.g. `youtube.com/results?search_query=`, `spotify:search:`), Playwright over CDP to the user's own Chrome.
- **Speed is the #1 priority.** Most commands should finish in ~1–3 s.
- **Local-first**: user data stays on the machine.
- LLM provider must be **swappable** behind one interface (Claude via Anthropic API for prototyping, local models via Ollama/vLLM later, e.g. Qwen2.5-VL / UI-TARS).

## 3. Architecture — 4-tier execution ladder
Every command tries the fastest tier first; falls down only on failure/low confidence.

| Tier | Mechanism | Target latency | Handles |
|---|---|---|---|
| 1. Instant intents | Router → direct OS action | ~0.3 s | open app, YouTube search URL, volume, file search |
| 2. Learned skills | Replay saved action sequence from a past success | ~1–2 s | anything done before |
| 3. Structured agent | LLM reads **UIA tree / DOM as text** (no images), plans **3–5 actions per call** | ~2–5 s | new tasks in normal apps/sites |
| 4. Vision fallback | Screenshot + OmniParser (numbered boxes) + vision LLM | ~5–15 s | UIs with no accessibility data |

Agent loop (Tiers 3–4): **See → Think → Act → Check**, verify at checkpoints, not after every click.

**Self-improvement:** every successful Tier 3/4 run is saved as a skill → next time runs at Tier 2. Skills with too many failures are auto-retired and re-learned.

### Speed principles (apply everywhere)
1. Stream STT; **act on partial transcripts** (start launching Spotify as soon as "…on Spotify" is heard).
2. **Keyboard over mouse** (Win+R, Ctrl+L, Ctrl+K, Tab, Ctrl+S, Ctrl+Enter).
3. **URL shortcuts** to jump directly to the right screen.
4. **Text over pixels** — UIA tree / DOM before screenshots.
5. **Batch planning** — multiple actions per LLM call.
6. **Event-based waits** (wait for element), never fixed sleeps where avoidable.
7. **Pre-warm** — Chrome connected via CDP, models loaded at startup, hot skills cached in memory.
8. **Two brains** — tiny fast router model; big model only when needed.
9. **Speak while acting** — TTS runs in parallel ("Opening Spotify…").

## 4. Query routing
### Query types
direct action · known task · new task · multi-step task · question (answer; search browser if needed) · screen question ("what's this error?") · control ("stop", "undo", "no, the other one") · ambiguous ("send it to him").

### Cascade
```
query
  → context resolution (rewrite "send it to him" → "send Resume.pdf to Arjun" using session history)
  → control word?                        → handle instantly (interrupt running task)
  → regex match?                         → Tier 1
  → embedding match > 0.85 (sqlite-vec)? → Tier 2 skill
  → small classifier conf > 0.8?         → Tier 1/2 with slots (intent + app + args)
  → else LLM planner                     → decompose multi-step into subtasks (each re-enters cascade)
                                           → Tier 3/4 or answer the question
  → too ambiguous                        → ask the user ONE short question
```
Log every route + outcome → becomes classifier training data.

## 5. Safety & human checkpoints (permanent design rule)
- 🟢 Auto: open apps, play media, search, read.
- 🟡 Confirm first (read back full details incl. exact email address/file/price): send messages/emails, submit forms, delete/move files, place orders.
- 💳 **One-tap checkout:** the agent may click **Pay** only after a spoken + on-screen confirmation of item, quantity, price, delivery address and payment method. It uses only payment methods already saved on the site. It **never stores** card numbers, CVV or UPI PIN. **OTP / UPI PIN / 3-D Secure are always entered by the user.**
- 🔴 Always hand control to user: **OTPs, UPI PINs, passwords, logins, CAPTCHAs / any "verify you are human" check**, system settings changes.
- **Never auto-solve CAPTCHAs.** Detect (iframes from recaptcha / hcaptcha / challenges.cloudflare.com, or vision sees the check) → pause & save state → bring window to front + speak/notify → wait until it disappears → resume. Minimize them by using the user's real logged-in Chrome profile via CDP, never headless, human-paced actions.
- Global **kill hotkey (Esc)** stops the agent instantly. Mic stays live during execution so "stop" / corrections can interrupt.

## 6. Storage (local, encrypted)
```
%LOCALAPPDATA%\JarvisAgent\
├── agent.db    SQLite + SQLCipher (key protected via Windows DPAPI)
├── vectors     sqlite-vec embeddings (semantic match of queries → skills/history)
├── skills\     JSON action sequences
└── logs\       debug steps + screenshots, auto-purged after 7 days
```
```sql
commands(id, ts, transcript, intent, tier_used, status, duration_ms)
skills(id, name, trigger_embedding, app, steps_json, success_count, fail_count, last_used)
memory(key, value, source, updated_at)   -- "contact:arjun" → "arjun@gmail.com"
```
**Never store:** passwords, OTPs, card/UPI details, password-field input (check UIA `IsPassword`), long-term screenshots. Redact before logging.
User controls: history panel (view/delete), pause memory, forget everything.

## 7. App-specific notes
- **Browser (Chrome):** user launches Chrome with `--remote-debugging-port=9222`; Playwright `connect_over_cdp`. Google blocks logins in automated browsers, so always reuse the real profile.
- **Amazon:** search box `#twotabsearchtextbox`; results `div[data-component-type='s-search-result']`; LLM picks best of top 5 → confirm (one-tap checkout, §5) → Pay → user enters OTP / UPI PIN.
- **Gmail (UI):** Compose `div[gh='cm']`, To `input[aria-label='To recipients']`, Subject `input[name='subjectbox']`, Body `div[aria-label='Message Body']`, attach via `set_input_files` on `input[type='file'][name='Filedata']`, wait for attachment to show before send, Send `Ctrl+Enter`.
- **Spotify (desktop UI):** launch → `Ctrl+L`/`Ctrl+K` focus search (or `spotify:search:<q>`) → type → click Top result play via UIA/vision → verify now-playing bar.
- **Word:** `win32com.client.Dispatch("Word.Application")`, `Visible=True`, live `Selection.TypeText`. Dictation commands: "new paragraph", "bold that", "make a heading", "undo/delete that", "save as <name>" (`SaveAs2`), "stop dictation". Distinguish command vs content: exact phrases → wake-word prefix → LLM classifier for ambiguous. Optional LLM cleanup of punctuation/fillers.
- Keep all selectors/shortcuts in **config files**, not hardcoded; LLM + accessibility tree as fallback when selectors break.

## 8. Tech stack
- **Voice:** wake word (openWakeWord), Silero VAD, faster-whisper (streaming), Piper TTS. Later: Indian languages (IndicConformer etc.).
- **Desktop:** pywinauto / UI Automation, pyautogui, win32com, mss (screenshots).
- **Browser:** Playwright over CDP.
- **Vision fallback:** OmniParser + vision LLM.
- **Brain:** small router (embeddings + classifier) + planner LLM via provider-agnostic interface (PydanticAI preferred). First provider: Claude (Anthropic API).
- **Backend:** Python 3.12 (asyncio), local FastAPI/websocket for UI. Env/deps managed with **uv**.
- **Shell UI:** Tauri tray app — global hotkey, status overlay, confirmation dialogs, history panel.
- **Storage:** SQLite + SQLCipher + sqlite-vec.

## 9. Folder structure
```
PersonalAgent/
├── CLAUDE.md
├── pyproject.toml
├── config/
│   ├── settings.yaml          # models, thresholds, hotkeys
│   ├── intents.yaml           # Tier-1 regex intents
│   └── apps/                  # per-app selectors & shortcuts (amazon.yaml, gmail.yaml, spotify.yaml, word.yaml)
├── src/jarvis/
│   ├── main.py                # entry: starts voice loop + services
│   ├── config.py              # settings loader (YAML → Pydantic)
│   ├── voice/                 # wakeword.py, vad.py, stt.py (streaming), tts.py
│   ├── router/                # context.py, cascade.py, regex_router.py, embed_router.py, classifier.py
│   ├── planner/               # llm.py (provider interface), planner.py, decomposer.py
│   ├── tiers/
│   │   ├── instant/           # app_launcher.py, system.py (volume/brightness), files.py, urls.py
│   │   ├── skills/            # store.py, replay.py, learner.py
│   │   ├── structured/        # uia_agent.py, browser_agent.py, observer.py (UIA tree/DOM → text)
│   │   └── vision/            # screenshot.py, omniparser.py, vision_agent.py
│   ├── executors/             # keyboard_mouse.py, playwright_cdp.py, office_com.py
│   ├── apps/                  # word_dictation.py, gmail_flow.py, amazon_flow.py, spotify_flow.py
│   ├── safety/                # policy.py (tiers 🟢🟡🔴), confirm.py, captcha.py, killswitch.py, redact.py
│   ├── memory/                # db.py, crypto.py (DPAPI), vectors.py, history.py
│   └── telemetry/             # logging, metrics
├── ui/                        # Tauri app
├── evals/                     # tasks.yaml (~50 tasks), runner.py → success rate, latency per tier
└── tests/
```

## 10. Build roadmap (phases)
| # | Phase | Deliverable |
|---|---|---|
| 0 | Scaffold | uv project, folder skeleton, config loader, logging, latency timer, ruff/mypy/pytest |
| 1 | Core models + safety skeleton | Pydantic Intent/Action/ActionResult, policy.py, Esc kill-switch, redact.py |
| 2 | Tier-1 instant tools (text input) | intents.yaml regex router + ~10 tools |
| 3 | Voice out | Piper TTS, speak-while-acting |
| 4 | Voice in | openWakeWord + Silero VAD + streaming faster-whisper → router |
| 5 | Browser via CDP | Playwright CDP, YouTube flow, CAPTCHA handoff |
| 6 | Amazon flow | search → LLM pick → one-tap checkout |
| 7 | LLM provider interface | PydanticAI, Anthropic first, Ollama stub |
| 8 | Gmail flow | compose/attach/read-back/confirm/send |
| 9 | Spotify via UIA | pywinauto + `spotify:search:` |
| 10 | Word dictation via COM | live typing + dictation commands |
| 11 | Tier-3 structured agent | UIA/DOM observer, batched See→Think→Act→Check |
| 12 | Skills (Tier 2) | learn, replay, retire |
| 13 | Storage | SQLCipher + DPAPI, sqlite-vec, history |
| 14 | Router cascade | context resolution, embeddings, classifier, decomposer |
| 15 | Tier-4 vision | mss + OmniParser + vision LLM |
| 16 | Eval suite + Tauri UI | ~50 tasks, metrics; tray app |
| Later | Indian-language voice, fine-tuned router, fine-tuned UI-grounding model |

## 11. Coding conventions
- Python 3.12, type hints everywhere, Pydantic models for actions/intents/plans.
- Async where I/O-bound; never block the voice loop.
- Every action executor returns a structured result (`ok`, `error`, `observation`) for verification.
- Every risky action goes through `safety/policy.py` — no bypass paths.
- Measure latency per stage (`telemetry.metrics.timed`); optimize for speed first.
- Prefer small, testable modules; add an eval task for every new capability.
- Run before handing a phase over: `uv run pytest`, `uv run ruff check .`, `uv run mypy src tests`.
