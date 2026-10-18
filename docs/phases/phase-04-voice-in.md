# Phase 4: Voice in (wake word, VAD, Whisper)

**Goal:** say **"Hey Jarvis, open Spotify"** and it happens. Answer confirmations by voice, and say
**"stop"** to cancel a running task. Everything runs locally; audio never leaves memory.

## How to use it

```powershell
uv run jarvis run            # voice mode (first run downloads Whisper small.en, ~480 MB)
uv run jarvis listen-test    # mic check: prints what it hears, executes nothing
```

| You do | What happens |
|---|---|
| "Hey Jarvis, open Spotify" (one breath is fine) | beep → transcript → Spotify opens |
| Press **Ctrl+Alt+J**, then speak | same, without the wake word (noisy rooms) |
| "stop" / "cancel" / "never mind" while a task runs | task cancelled, speech stops — no wake word needed |
| A confirmation is read out ("Send email to Arjun?") | say "yes" / "no"; unclear twice or silence → **no** |
| A handoff ("Enter the OTP yourself") | do it, then say "done" (or "cancel") |
| "Hey Jarvis" while Jarvis is talking | it stops talking and listens (barge-in) |
| **Esc** | still kills the running task, as before |

## What was built

| File | Purpose |
|---|---|
| `src/jarvis/voice/mic.py` | `Microphone`: 16 kHz mono int16, 80 ms frames, PortAudio thread → asyncio queue (bounded, drops oldest). `ArraySource`: a recording played as if it were the mic (tests/evals) |
| `src/jarvis/voice/wakeword.py` | `OpenWakeWord`: the bundled `hey_jarvis` ONNX model |
| `src/jarvis/voice/vad.py` | `SileroVAD`: Silero VAD (the ONNX copy bundled with openWakeWord) |
| `src/jarvis/voice/stt.py` | `WhisperSTT`: faster-whisper, CUDA float16 → CPU int8 fallback, hotwords, prewarm, cuBLAS DLL setup |
| `src/jarvis/voice/listener.py` | `ListenerCore` (pure state machine, frame in → event out) + `Listener` (async: mic → core → Whisper off the event loop, live partials, hallucination filter) |
| `src/jarvis/voice/replies.py` | `parse_yes_no`, `parse_handoff`, `is_stop` |
| `src/jarvis/voice/session.py` | `VoiceConfirmer` (answers from the next utterance) + `VoiceSession` (listener → agent; mic live during tasks; "stop"; barge-in) |
| `src/jarvis/voice/runtime.py` | Builds everything for `jarvis run` / `jarvis listen-test`; push-to-talk hotkey; hotwords from `aliases.yaml` |
| `src/jarvis/voice/sounds.py` | In-memory wake/done beeps |
| `src/jarvis/voice/speaker.py` | `is_speaking` (the mic ignores Jarvis's own voice) |
| `src/jarvis/safety/killswitch.py` | `trigger(by=…)` so "stop" by voice reports `stopped by voice`; `active` |
| `config/settings.yaml`, `src/jarvis/config.py` | `listen:` (wake, thresholds, timings, beep, partials), `stt:` (models, device, hotwords), `hotkeys.push_to_talk` |
| tests | `test_listener.py`, `test_replies.py`, `test_session.py`; desktop: real wake+VAD+Whisper on Piper speech |

New dependencies: `faster-whisper` (CTranslate2), `openwakeword` 0.4.0 (pure ONNX, bundles the
`hey_jarvis` and Silero VAD models — newer releases need `tflite-runtime`, which has no Windows
build), `nvidia-cublas-cu12` (cuBLAS for the GPU; cuDNN already ships with CTranslate2).

## How it works

```
mic (80 ms frames)
  └─► ListenerCore.feed(frame)
        IDLE       wake score ≥ 0.5 → Wake ──► beep, stop Jarvis talking
                   push-to-talk flag → Wake(ptt)
                   mode "open" + VAD speech + Jarvis not talking → start recording (with 240 ms pre-roll)
        RECORDING  VAD per frame; 700 ms of silence after speech → Recorded (trailing silence trimmed)
                   nothing said in 4 s → NoSpeech · 15 s max → Recorded
  └─► Listener: Whisper in a worker thread → Heard(text) (hallucinations like "Thank you." dropped)
                every 0.8 s while recording (GPU only): partial transcript, shown live
  └─► VoiceSession.on_heard(text)
        confirmation pending? → it's the answer
        "stop"?               → kill switch (if a task runs) + stop speaking
        task running?         → ignored ("busy — say stop")
        open-mic chatter?     → ignored (not addressed to Jarvis)
        else                  → asyncio task: Agent.handle(text); mic switched to "open" until done
```

**Why "open" mode.** While a task runs or a confirmation is pending, any speech is transcribed, so
"stop" and "yes" work without the wake word. While Jarvis itself speaks, open-mode speech is ignored
(otherwise it would hear its own "Send email to Arjun? …" and answer itself) — the wake word still
works, so "Hey Jarvis, stop" always gets through.

**Voice confirmations default to NO.** Unclear twice ("hmm", or a sentence with both "yes" and
"no" words, like "don't send it"), or 20 s of silence → the action is cancelled. A missed yes costs
one retry; a misheard yes could send an email.

## Design decisions

| Decision | Why |
|---|---|
| openWakeWord 0.4.0, bundled models | Pure ONNX, no downloads, ~2 ms/frame on CPU; "Hey Travis" scores 0.30 vs 1.00 |
| Reuse its Silero VAD copy | No PyTorch (~2 GB) just for VAD; ~0.5 ms/frame |
| `small.en` on CUDA, `base.en` on CPU | 4050 runs small.en in ~90 ms; CPU fallback keeps it working anywhere |
| Greedy decoding, no timestamps, our own VAD | Commands are short; every option here trades nothing for speed |
| Hotwords from `aliases.yaml` | Whisper heard "lofi" as "Luffy" without them |
| Pure `ListenerCore` | The whole state machine is unit-tested with scripted frames, no audio or threads |
| Prewarm with a dummy run | The first CUDA inference is ~5× slower; pay it at startup |
| Fail at load, not mid-command | A missing cuBLAS DLL surfaces during prewarm (and falls back to CPU) |
| Never speak the wake word | "Jarvis is ready" could wake Jarvis; it says "Ready." |
| Partials shown, not acted on | Acting on partial transcripts needs accuracy data first (see limitations) |
| Push-to-talk Ctrl+Alt+J | Ctrl+Space / Ctrl+Shift+Space clash with VS Code and IMEs |

## Measured on the dev machine (RTX 4050 Laptop, 6 GB)

| | Time |
|---|---|
| Whisper `small.en` per command (1–2.6 s of audio) | **86–110 ms** |
| Whisper `base.en` on CPU (2.7 s clip) | ~450 ms |
| Stop talking → text ready | ≈ 700 ms pause detection + ~90 ms ≈ **0.8 s** |
| Startup: Whisper load + warm-up / wake + VAD | 1.5 s / 1.4 s (in parallel with Piper + app index) |
| Wake word / VAD per 80 ms frame | ~2 ms / ~0.5 ms (CPU) |
| `small.en` download | ~480 MB, once |

End-to-end on synthetic speech (Piper → real wake word + VAD + Whisper): 5/5 woke, 4/5 transcripts
exact ("arijit singh" → "aridget sing").

## How to test

```powershell
uv run pytest                                  # unit tests (scripted frames, fake STT/agent)
uv run pytest -m desktop -k voice_pipeline     # real models on Piper-generated speech, no mic needed
uv run jarvis listen-test                      # your mic, prints transcripts + timings
uv run jarvis run -v                           # full voice mode with policy logs
```

## Limitations / next

- **Not yet tested with a real human voice in this phase's checks** — the automated end-to-end
  tests use Piper's voice. `listen-test` is the way to tune `wake_threshold` and `silence_ms` for
  your mic and room.
- Indian names and words are often misheard by English Whisper. Add them to `stt.hotwords`; proper
  Indian-language STT is on the roadmap.
- 700 ms of silence ends a command; lower `silence_ms` for speed, raise it if you get cut off.
- Partial transcripts are only displayed. Acting on them early ("…on Spotify" → start launching) is
  deferred until we have accuracy numbers from real use.
- In voice mode there is no typed fallback for confirmations; Esc still works.
- Speakers that are loud and close to the mic can leak Jarvis's voice into "open" mode right after it
  stops talking; a headset avoids this. Echo cancellation is a later improvement.
- Next: Phase 5 — browser via CDP (YouTube playback, CAPTCHA handoff).
