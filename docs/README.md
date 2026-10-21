# Jarvis documentation

| Doc | What's in it |
|---|---|
| [architecture.md](architecture.md) | How the whole system fits together, and what is built so far |
| [safety.md](safety.md) | Safety layer reference: risk levels, rules, confirmations, kill switch, redaction |
| [development.md](development.md) | Setup, commands, project layout, conventions |
| [refinements.md](refinements.md) | Small improvements found in testing, to do later |
| [phases/](phases/) | One doc per build phase: what was built, how, and why |

## Phases

| # | Phase | Status | Doc |
|---|---|---|---|
| 0 | Project scaffold | ✅ Done | [phase-00-scaffold.md](phases/phase-00-scaffold.md) |
| 1 | Core models + safety layer | ✅ Done | [phase-01-safety.md](phases/phase-01-safety.md) |
| 2 | Tier-1 instant tools | ✅ Done | [phase-02-instant.md](phases/phase-02-instant.md) |
| 3 | Voice out (TTS) | ✅ Done | [phase-03-tts.md](phases/phase-03-tts.md) |
| 4 | Voice in (wake word, VAD, Whisper) | ✅ Done | [phase-04-voice-in.md](phases/phase-04-voice-in.md) |
| 5 | Browser via CDP (YouTube, tabs, CAPTCHA handoff, page risk) | ✅ Done | [phase-05-browser.md](phases/phase-05-browser.md) |
| 7 | LLM layer: rules → Claude Haiku via OpenRouter / local model, benchmark | ✅ Done | [phase-07-llm.md](phases/phase-07-llm.md) |
| 11 | **General browser agent** — any website, no site-specific code | ✅ Done | [phase-11-agent.md](phases/phase-11-agent.md) |
| 11.2 | **General desktop agent** — any Windows app via UI Automation; Spotify is just a task | ✅ Done | [phase-11.2-desktop.md](phases/phase-11.2-desktop.md) |
| 11.3 | **Files in and out** — attach / upload on any site or app, downloads, open files | ✅ Done | [phase-11.3-files.md](phases/phase-11.3-files.md) |
| 11.4 | **Checkout guard** — any shop: the order summary is read off the page word for word, read back, placed only on your yes | ✅ Done | [phase-11.4-checkout.md](phases/phase-11.4-checkout.md) |
| 15 | **Vision** — when text isn't enough: a screenshot, OmniParser boxes on the GPU, Haiku picks; same safety | ✅ Done | [phase-15-vision.md](phases/phase-15-vision.md) |
| 12 | **Skills** — a task that worked is learned automatically; the next one of its kind is replayed in seconds with no model call per step, same safety | ✅ Done | [phase-12-skills.md](phases/phase-12-skills.md) |
| 10 | Live dictation into any text box (Word, Notepad, Gmail, WhatsApp…) | Planned | — |
| 13–16 | See roadmap in [../CLAUDE.md](../CLAUDE.md#10-build-roadmap-phases) (includes 11b shell commands, 14b learned risk classifier) | Planned | — |
