# Phase 7: LLM layer (rules first, then OpenAI / Claude or a local model)

**Goal:** understand any phrasing — "make it louder bro", "fast forward ten minutes", "what all can
you do" — without writing a regex for each, while staying fast, private where possible, and working
offline. The LLM **chooses among the same intents** the rules use, so every action still goes
through the mapper → `ActionRunner` → `SafetyPolicy` → executor. Nothing new can be executed.

## How a command is understood

```
                 ┌─ match ─► Intent(source="regex")                      ~0.1 ms
text ─► rules ───┤
                 └─ miss ──► LLM router
                               offline → local model (Ollama, on the GPU)
                               online  → OpenRouter: Claude Haiku 5.5 (default) · OpenAI · Claude · local  ← your choice
                               (then the others if it fails; an unavailable one is skipped 30 s)
                             RouterDecision:
                               run_intent {intent, slots} → validated → Intent(source="llm") → act
                               answer / clarify / unsupported → spoken, nothing is done
```

- **Modes** (`router.mode`): `hybrid` (default, above) and `llm_first` (the LLM first, rules only if
  every model fails) — kept to measure the difference.
- **Switching by voice:** "use openrouter / the free model", "use openai / chatgpt", "use claude",
  "use the local model", "which model are you using / are you online".
- **Why OpenRouter → Claude Haiku 5.5 is the default online model:** the account has OpenRouter
  credits (not Claude / OpenAI directly), and Haiku won the paid benchmark below. One key
  (`OPENROUTER_API_KEY`) reaches every model; switch with `llm.openrouter_model`. Free fallback:
  `nvidia/nemotron-3-super-120b-a12b:free`. Direct OpenAI / Claude providers stay supported.
- **"One moment…"** is spoken if the model takes longer than 0.6 s, so there's never dead air.
- **Follow-ups:** the last 5 turns (redacted) go with every LLM call — "a bit more", "close it now".

## What was built

| File | Purpose |
|---|---|
| `src/jarvis/planner/schema.py` | `RouterDecision`: kind (`run_intent`/`answer`/`clarify`/`unsupported`), intent, slots (a list — strict-schema friendly), text |
| `src/jarvis/planner/catalog.py` | The LLM's menu, **generated from `intents.yaml`**: descriptions, slots (required/optional, from the action templates), examples; deterministic rendering; `to_intent()` validation incl. **slot-value checks** (numbers are numbers, units real and matching the words said, choices from their list, no bare it/that, nothing said left out) |
| `src/jarvis/planner/llm.py` | `OpenRouterProvider` (OpenAI-compatible API at openrouter.ai, `OPENROUTER_API_KEY` only — never falls back to the OpenAI key; free-limit message), `OpenAIProvider` (official `openai` SDK, `chat.completions.parse` with `RouterDecision` as a strict JSON schema, temperature 0; automatic prompt caching; "no credits" / no key / offline mapped to plain words), `ClaudeProvider` (official `anthropic` SDK, `messages.parse` + structured output, cached system prompt, effort low) and `OllamaProvider` (official `ollama` client, JSON constrained to the schema, temperature 0, model kept loaded); `LLMUnavailable(reason, sticky)` |
| `src/jarvis/planner/router.py` | `LLMRouter`: provider order (online/offline + choice), failover, skip-unavailable-for-30 s, validation + one retry with the problem explained |
| `src/jarvis/planner/connectivity.py` | Online check (1 s TCP to the Claude API, cached 30 s; a failed Claude call = offline for 60 s) |
| `src/jarvis/router/context.py` | Last 5 redacted turns |
| `src/jarvis/router/log.py` | Every decision → `%LOCALAPPDATA%\JarvisAgent\logs\routes.jsonl` (redacted) — Phase 14 training data |
| `src/jarvis/executors/llm_control.py` | "use claude" / "use the local model" / status — through `ActionRunner` (GREEN) |
| `src/jarvis/agent.py` | Both modes, spoken answers, "One moment…", context, route log, prewarm (imports the SDK, loads the local model) |
| `src/jarvis/planner/evaluate.py` + `evals/router_cases.yaml` | The benchmark (below) |
| `src/jarvis/router/regex_router.py` | Never acts on a bare pronoun slot ("open it" ≠ open an app called "it"); `examples`, `slots`, `optional`, `llm` fields |
| `config/intents.yaml` | Examples + slot docs for every intent; `use_claude`, `use_local_model`, `llm_status` |
| `config/settings.yaml` | `router.mode`; `llm: online_provider, router_model, local_model, ollama_url, effort, timeout_s` |
| `src/jarvis/main.py` | `.env` loading; `jarvis llm status`, `jarvis llm ask "…"`, `jarvis eval-router` |

New dependencies: `openai`, `anthropic`, `ollama`, `python-dotenv`.

## Design decisions

| Decision | Why |
|---|---|
| Rules first | 0.1 ms, free, offline, deterministic for the commands said most often |
| LLM picks from the intent list only | Can't invent actions; safety layer unchanged |
| Catalog generated from `intents.yaml` | One source of truth; a new intent is instantly understood by the LLM too |
| Official SDKs, not PydanticAI | One structured call is all we need; fewer dependencies; Ollama is swappable behind the same interface |
| OpenAI gpt-4o-mini online (default) | User's account has OpenAI credits, not Claude; a small model is enough to pick an intent |
| Claude Haiku 5.5 kept | Switchable any time; the interface makes providers interchangeable |
| One schema for all providers | OpenAI's strict-schema converter accepts `RouterDecision` as is (all fields required, defaults stripped) — no separate model |
| Local model always the offline fallback; user-selectable online | Works without internet; private option; decided by measurement, not guesswork |
| Validation + one retry | A wrong intent/slot name is corrected by the model itself before anything runs |
| Skip an unavailable provider for 30 s | Windows takes ~2 s to report a refused connection; no key / no Ollama shouldn't cost seconds per command |
| Bare pronouns never match a rule | "open it" / "close that" used to act on an app called "it" — found by the benchmark |
| …nor a slot that *starts* with one | "play it a bit quicker", "launch that insta thing", "start this video from the beginning" were grabbed by catch-all rules |
| Check slot values, not just names | The first real run: qwen put "bytes" as a volume, "seconds" for "10 min", and left amounts out so the 10-second default won |
| Warm the local model with its own 120 s timeout | Its first-ever load took 39 s; an 8 s timeout made it look "not running" |
| `open X` with an X nothing resolves to → ask the LLM | User test: "open mr beast channel in youtube" opened youtube.com home via the rules; now → YouTube search "mr beast channel" via the LLM |
| Fuzzy names = typos, not sentences | The app matcher matched any long phrase *containing* an app name; now a fuzzy match needs whole-string similarity or a name-length query |
| Every decision logged (redacted) | Phase 14 trains a small local classifier from it → fewer LLM calls over time |

## Benchmark (`jarvis eval-router`)

72 sentences in 8 categories: exact commands, paraphrases, speech mishearings, Indian-English
phrasing, follow-ups, questions (must answer, not act), unsupported (must refuse), vague (must ask).
Scored per target: correct (alone / hybrid), **wrong actions** (it did something it shouldn't have —
the most important number), p50/p95 latency, $ per 1000 commands.

Measured 2026-10-19 on the RTX 4050 (local models free, on the GPU):

| Target | Correct (alone) | Correct (hybrid) | Wrong actions (hybrid) | p50 / p95 |
|---|---|---|---|---|
| rules | 25% | 25% | **4%** | 0 ms |
| qwen2.5:3b — first run | 68% | 69% | 22% | 680 / 1090 ms |
| **qwen2.5:3b — after the slot checks** | 69% | **76%** | **10%** | 677 / 1413 ms |
| llama3.2:3b — after the slot checks | 62% | 64% | 21% | 676 / 1279 ms |

qwen2.5:3b by category (alone): Indian English 100%, vague 100%, unsupported 83%, questions 80%,
exact 73% (the rules cover those), paraphrases 64%, mishearings 33%, follow-ups 33%. It stays the
local default. llama3.2:3b acts on unsupported requests ("delete my downloads" → open downloads), so it
isn't used.

OpenRouter (free) vs the local model, 2026-10-19, `--per-tag 5` (36 cases — fits the free daily limit):

| Target | Correct (alone) | Correct (hybrid) | Wrong actions (hybrid) | p50 / p95 |
|---|---|---|---|---|
| rules | 19% | 19% | 6% | 0 ms |
| **OpenRouter `nvidia/nemotron-3-super-120b-a12b:free`** | **92%** | **89%** | **8%** | 3756 / 9519 ms |
| local qwen2.5:3b | 69% | 75% | 14% | 638 / 1346 ms |

OpenRouter alone: 100% on exact, paraphrase, Indian English, unsupported and vague; 80% mishearings and
questions; 67% follow-ups. Its 3 misses: "play lo fi on you tube" → query "lo fi" (fine — strict scorer),
"skip this one" → skip_ad, "can you control spotify" → unsupported instead of an answer.

Paid models through OpenRouter (credits added), 2026-10-19, all 72 cases (whole run ≈ $0.06):

| Target (hybrid with rules) | Correct | Wrong actions | p50 / p95 | $ / 1000 commands | Mishearings |
|---|---|---|---|---|---|
| **anthropic/claude-haiku-5.5** | **92%** | **6%** | 1696 / 2334 ms | $0.34 | **83%** |
| openai/gpt-4o-mini | 92% | 6% | 1220 / 1833 ms | $0.21 | 50% |
| google/gemini-2.5-flash-lite | 89% | 6% | 1004 / 2775 ms | $0.22 | 83% |
| openai/gpt-4.1-nano | 74% | 8% | 956 / 1264 ms | $0.08 | 17% |
| nemotron :free | — (hit a rate limit after 53 calls) | | 3147 / 9007 ms | $0 | |
| local qwen2.5:3b | 79% | 11% | 841 / 1941 ms | $0 | 50% |

Haiku's misses: "show me how to make pasta on youtube" → search (not play); "get rid of notepad" →
asks which; "lo fi" (strict scorer); "can you control spotify" → unsupported instead of an answer.

**Final decision (user, 2026-10-19): OpenRouter → `anthropic/claude-haiku-5.5`** — tied best on
accuracy and wrong actions, best on misheard speech (matters most in voice mode), within the 1–3 s
target, ~$0.0003 per LLM-routed command.

Earlier decision with only the free model (superseded):** OpenRouter is the online default** — far fewer wrong actions; the cost is
speed (~4 s, up to ~10 s; "One moment…" covers the wait). Exact commands never reach it (rules, ~0 ms).
After the free limit (~50/day) Jarvis uses the local model automatically. OpenAI / Claude: when there
are credits.

## Where the time goes (measured 2026-10-19, Haiku 5.5 via OpenRouter)

Asked: would a persistent (WebSocket-style) connection make it fast? Measured — mostly no:

| Part of one call | Time |
|---|---|
| Opening a new HTTPS connection (TCP + TLS) | ~166 ms, once — the SDK keeps it open and reuses it |
| First call on a fresh connection | ~2.6 s |
| Later calls, same connection | ~1.6 s (strict schema) — the model reading the prompt and answering |
| …time to first token / writing ~30 tokens (plain request) | ~820 ms / ~230 ms |

OpenRouter/Anthropic have no WebSocket chat API; the HTTP keep-alive the SDK already does gives the
same benefit. What was added instead:

- **Prompt caching** — `OpenRouterProvider` marks the (identical every call) system prompt with
  `cache_control` for `anthropic/*` models: 3003 of 3029 prompt tokens cached from the 2nd call
  (~90% cheaper input). It doesn't make the call faster.
- **Warm-up at startup** (`repl` / voice mode): one tiny request opens the connection and writes the
  cache, so the first real command doesn't pay the ~1 s cold penalty.
- `jarvis llm ask "…" --repeat N` shows cold vs warm and cached tokens.

Benchmark re-run with caching on (all 72 cases, Haiku 5.5 via OpenRouter):

| | Correct (hybrid) | Wrong actions | p50 / p95 | $ / 1000 commands |
|---|---|---|---|---|
| before caching | 92% | 6% | 1696 / 2334 ms | $0.336 |
| **with caching** | **93%** | **6%** | 1863 / 2618 ms | **$0.067** (−80%) |

Same answers (differences are run-to-run noise); 5× cheaper. (A first re-run attempt failed halfway on
a temporary OpenRouter outage — the router then marked it offline and fell back, as designed.)

Also measured, not adopted (user kept the strict schema): plain JSON mode answered in a steady ~1.15 s
vs 1.25–3.6 s with the strict JSON schema; our own validation would still check every answer.

## How to test

```powershell
uv run pytest                                   # fake providers, no network
uv run jarvis llm status                        # key? Ollama? model pulled? online?
uv run jarvis llm ask "make it louder bro"      # what each model decides (nothing runs)
uv run jarvis eval-router --providers rules,local --local-models qwen2.5:3b,llama3.2:3b   # free
uv run jarvis eval-router --providers rules,openrouter --per-tag 5                        # free tier: ~50/day
uv run jarvis repl                              # "what all can you do", "fast forward ten minutes"
```

## Setup

```powershell
# Create C:\Games\PersonalAgent\.env (gitignored) with
OPENROUTER_API_KEY=sk-or-...      # free models work without credits
OPENAI_API_KEY=sk-...             # optional (needs OpenAI credits)
ANTHROPIC_API_KEY=sk-ant-...      # optional (needs Claude credits)
# Local model:
winget install Ollama.Ollama
ollama pull qwen2.5:3b          # ~2 GB; optionally also llama3.2:3b to compare
```

## Limitations / next

- Still ~10% wrong actions in hybrid with the local model (e.g. "can you control spotify" opens
  Spotify). Fine for now — every action still goes through the safety policy; Phase 14's classifier
  and more examples should push it down.
- OpenRouter free models: ~50 requests/day without credits; then Jarvis falls back to the local model.
- Haiku via OpenRouter: ~1.7 s typical — inside the 1–3 s target but not instant; Phase 14's local
  classifier should take most of this load. Each LLM-routed command costs ~$0.0003.
- "Clarify" questions are spoken; the next sentence is understood as a new command (with context), not
  as a direct answer to the question.
- Esc doesn't cancel a pending LLM call (it's at most a few seconds); it does cancel the action after.
- Next: **Phase 6 — Amazon** (search → the LLM picks the best of the top results → one-tap checkout).
