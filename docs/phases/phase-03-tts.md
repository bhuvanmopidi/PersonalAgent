# Phase 3: Voice out (text-to-speech)

**Goal:** Jarvis speaks while it works, reads confirmations aloud, and goes quiet on Esc. Everything
runs locally.

## What it says

| Situation | Spoken | Example |
|---|---|---|
| Starting an action | The intent's `description` (in parallel with the action) | "Opening Spotify" |
| Level changes (`speak: result`) | The result instead of the description | "Volume 60%" |
| Failure | The observation (replaces the start phrase) | "I couldn't find an app or site called 'blahblah'" |
| No match | "I can't do that yet." | |
| Confirmation / handoff | The prompt, before asking | "Send email to arjun@gmail.com?" |
| Declined / cancelled / killed by Esc | Nothing (and Esc stops speech immediately) | |

Successes aren't followed by "done"; that gets annoying fast.

## What was built

| File | Purpose |
|---|---|
| `src/jarvis/voice/tts.py` | `TTSEngine` protocol; `PiperEngine` (neural, local), `SapiEngine` (built-in Windows voice), `LazyEngine` (load on first use / prewarm), `create_engine()` (falls back to SAPI if the Piper voice is missing) |
| `src/jarvis/voice/audio.py` | `SoundDeviceSink`: one output stream per utterance, written in 50 ms blocks so stop is instant |
| `src/jarvis/voice/speaker.py` | `Speaker`: background speech, newest-wins replacement, phrase cache, redaction, time-to-first-audio metric |
| `src/jarvis/voice/voices.py` | Voice storage (`%LOCALAPPDATA%\JarvisAgent\voices`), download (atomic via a temp folder), list |
| `src/jarvis/safety/confirm.py` | `SpokenConfirmer`: speaks the prompt, then asks the inner confirmer |
| `src/jarvis/agent.py` | Speaks while acting; `prewarm()` loads the app index and the voice in parallel |
| `src/jarvis/router/regex_router.py`, `config/intents.yaml` | `speak: description \| result \| none` per intent |
| `src/jarvis/config.py`, `config/settings.yaml` | `tts: {engine, voice, speed}` |
| `src/jarvis/main.py` | `jarvis say`, `jarvis voices list/download`, `--mute` on `do`/`repl` |
| `tests/test_speaker.py`, `tests/fakes_voice.py`, `tests/test_agent.py`, `tests/test_desktop.py` | Speaker behaviour with fake engine/sink; agent speech rules; real Piper/SAPI synthesis |

New dependencies: `piper-tts` (pulls in `onnxruntime`), `sounddevice`, `numpy`. SAPI uses `pywin32`, which was already installed.

## How it works

```
Agent.handle("open spotify")
  speaker.say("Opening Spotify")   → returns immediately; a worker thread synthesizes + plays
  runner.run_action(...)           → runs at the same time
  failure? speaker.say(observation) → stops the old phrase, speaks the new one

Speaker._speak (worker thread)
  cached phrase? → play cached audio (0 ms synthesis)
  else engine.synthesize(text) yields ~1 chunk per sentence → sink plays each chunk as it arrives
  short phrase (≤ 60 chars) finished without interruption → cache it (LRU, 64 items)
```

**Stopping.** Each utterance has its own `threading.Event`. `stop()` sets it; the sink checks it
every 50 ms block and `abort()`s the stream, dropping buffered audio.

**Prewarming.** Loading Piper plus the first inference takes about 2.8 s, so `repl` (and the voice
loop in Phase 4) do it in the background at startup, together with the app index.

## Design decisions

| Decision | Why |
|---|---|
| Piper as the main engine | Natural neural voice, runs locally on CPU, ~40 ms to first audio once loaded |
| SAPI fallback | Works with zero downloads; a missing voice never means silence |
| Newest speech wins | Speech must never lag behind what's happening |
| Stream per sentence | Long replies start playing before they're fully synthesized |
| Phrase cache | Common phrases ("Opening Spotify", "Volume 60%") cost nothing the second time |
| Redact before speaking | An OTP that reaches an observation is never read aloud |
| TTS errors only logged | No audio device must never break an action |
| `speak` per intent in YAML | The speaking policy is config, not code |

## Measured on the dev machine

| | Time |
|---|---|
| Piper model load + warm-up | ~2.8 s (once, in the background) |
| Time to first audio, short phrase ("Volume 24%") | 35–50 ms |
| Time to first audio, longer sentence | ~260 ms |
| SAPI load / first audio | ~390 ms / ~44 ms |
| Voice download (`en_US-lessac-medium`, 60 MB) | ~28 s, once |

## How to test

```powershell
uv run jarvis voices download          # once: en_US-lessac-medium
uv run jarvis say "Opening Spotify"     # prints load and time-to-first-audio
uv run jarvis say --engine sapi "Hello"
uv run jarvis repl                      # speaks while acting; --mute to silence
uv run pytest                           # 161 unit tests (fake audio)
uv run pytest -m desktop                # real Piper + SAPI synthesis, Notepad, volume
```

## Limitations / next

- `jarvis do` doesn't prewarm, so the first phrase there arrives about 3 s late (the action itself isn't delayed). Long-running modes prewarm.
- Esc stops speech only while a task is running (the hook is task-scoped). Speech is short, and Phase 4 adds "stop" by voice.
- English voice only; Indian-language voices are planned for later.
- Phase 4: wake word + VAD + streaming Whisper, so you can speak commands and answer confirmations by voice.
