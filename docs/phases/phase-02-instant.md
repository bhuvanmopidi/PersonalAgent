# Phase 2: Tier-1 instant tools (typed commands)

**Goal:** a typed command is matched by a regex rule and done on the PC in milliseconds, with no AI.
Voice (Phase 4) will feed the same router.

## What you can say

| Example | Intent | What happens |
|---|---|---|
| "open notepad", "launch the chrome app", "open spotfy" | `open_app` | Launches from the app index (typos OK) |
| "open youtube", "go to github.com" | `open_app` | Site alias or domain (since Phase 5: opened in the Jarvis Chrome window) |
| "close notepad", "quit spotify" | `close_app` | Graceful close, like clicking ✕ (the app still asks about unsaved work) |
| "play lofi on youtube", "search youtube for X" | `youtube_search` | Opens the YouTube results page (actually playing comes in Phase 5) |
| "google X", "search for X" | `web_search` | Google results |
| "volume up [by 20]", "volume 30", "mute", "unmute" | `volume_*`, `mute`, `unmute` | Core Audio. Changing the volume also unmutes |
| "brightness 50", "brighter", "dim the brightness" | `brightness_*` | Built-in display (WMI) or DDC/CI monitors |
| "open downloads / documents / desktop / pictures / music / videos / home" | `open_folder` | Explorer, at the real folder location (respects OneDrive redirection) |
| "find my resume", "where is the project report file" | `find_file` | Searches Desktop, Documents and Downloads, then opens Explorer with the best match selected |
| "lock the screen" | `lock` | `LockWorkStation` |

Filler words are stripped first: "Hey Jarvis, please open Spotify!" becomes "open spotify".

## What was built

| File | Purpose |
|---|---|
| `config/intents.yaml` | Intents: regex patterns (named groups become slots) plus an action template and a description |
| `config/apps/aliases.yaml` | Spoken names → app names / process names / URIs / commands; site shortcuts |
| `src/jarvis/router/regex_router.py` | `normalize()`, `RegexRouter.route()` → `Intent` |
| `src/jarvis/tiers/instant/mapper.py` | `to_action()`: fills `{slot}` / `{slot\|url}` templates |
| `src/jarvis/tiers/instant/app_index.py` | `AppIndex`: all Start-menu apps from `Get-StartApps`, cached, with exact → prefix → fuzzy lookup |
| `src/jarvis/tiers/instant/apps.py` | `AppLauncher`: `resolve()`, `launch()`, `close()` |
| `src/jarvis/tiers/instant/system.py` | `Volume` (pycaw), `Brightness` (screen-brightness-control), `lock_screen()` |
| `src/jarvis/tiers/instant/files.py` | Known folders (registry), bounded fuzzy `find_files()`, reveal in Explorer |
| `src/jarvis/executors/os_executor.py` | `OSExecutor`: one handler per action kind, all returning `ActionResult` |
| `src/jarvis/agent.py` | `Agent.handle(text)`: router → mapper → `ActionRunner` (safety) → executor, under the kill switch |
| `src/jarvis/paths.py` | `%LOCALAPPDATA%\JarvisAgent` data and cache folders |
| `src/jarvis/main.py` | New CLI commands: `do`, `repl`, `apps` |
| `config/safety.yaml`, `safety/policy.py` | New GREEN kinds, `keyword_kinds`, more "irreversible" keywords |
| `evals/tasks.yaml` | 15 eval tasks |
| `tests/` | Router (28 utterances), mapper, apps, files, executor, agent, policy additions; `test_desktop.py` (real desktop) |

New dependencies: `pycaw`, `screen-brightness-control`, `rapidfuzz`, `psutil` (+ `types-psutil`).

## How it works

```
"Hey Jarvis, play lofi on YouTube"
  normalize          → "play lofi on youtube"
  RegexRouter.route  → Intent(youtube_search, {query: "lofi"})          ~0.1 ms
  to_action          → Action(open_url, "https://www.youtube.com/results?search_query=lofi")
  ActionRunner       → SafetyPolicy: GREEN → ALLOW
  OSExecutor         → os.startfile(url) → ActionResult(ok, "Opened …")
```

**Resolving "open X" — app or website?** (revised after Phase 3, see below)

The router strips filler ("open **up me the** instagram") and keeps a trailing **app / website /
site** as a `prefer` hint. `AppLauncher.resolve(name, prefer)` then decides:

| You say | Result |
|---|---|
| "open instagram.com" | typed domain → website |
| "open instagram **website**" | websites only (`prefer=web`) |
| "open whatsapp **app**" | installed apps only (`prefer=app`); fails if not installed |
| "open whatsapp" (no hint) | best match wins, in this order: exact app · exact site · prefix app · prefix site · fuzzy app · fuzzy site — so an **installed app beats the website**, and "insta" still finds instagram.com |
| "open github" | sites in `prefer_web` (Gmail, Amazon, GitHub…) open in the browser even if a same-named app (GitHub Desktop) is installed — exact site name only |

App/command aliases (`settings` → `ms-settings:`) are checked first; an exe on PATH is the last
resort. Apps are fuzzy-matched with WRatio (partial names: "chrome" → Google Chrome); sites use plain
whole-string ratio, so typos match ("instagarm") but "xbox" never matches the site "x" and
"google chrome" never matches the site "google". On a total miss the index is rebuilt once (apps
installed since the cache was built), at most every 5 minutes.

*Fix history:* originally sites were exact-match only, filler words weren't stripped and the
app/website word was discarded — "open me instagram" failed, and "open me youtube" only worked
because the YouTube Chrome app happened to be installed.

**App index.** `Get-StartApps` returns every Start-menu app, both Store and classic, with an AppID.
Every AppID launches the same way: `shell:AppsFolder\<AppID>` through ShellExecute (`os.startfile`).
That also handles UAC prompts for apps that need admin. Building the index takes about 2 s; it is
cached at `%LOCALAPPDATA%\JarvisAgent\cache\app_index.json`, and `jarvis repl` prewarms it in the
background.

**Closing apps.** It finds the processes by exe name (from the alias, e.g. "vs code" → `Code`) and
runs `taskkill /PID …` **without `/F`**. That sends WM_CLOSE, so apps can ask to save. Windows
processes (explorer, svchost, …) and **Jarvis's own process and its parents** (your terminal or
VS Code) are protected.

## Design decisions

| Decision | Why |
|---|---|
| Intents in YAML, first match wins | New commands need no code; order lets specific rules beat the generic "open X" |
| `fullmatch` after normalizing | No accidental partial matches; patterns stay readable |
| One launch path (`shell:AppsFolder`) | Store and classic apps behave the same; no per-app code |
| Exact → prefix → fuzzy (WRatio ≥ 85) | "notepad" doesn't open Notepad++; "spotfy" still works |
| Graceful close, protected list, self-protection | "close" never loses unsaved work or kills Windows or Jarvis itself |
| Keyword risk check only for UI-label kinds | "google how to delete my account" builds a URL containing "delete", but it's still only a search, so it stays GREEN. Unknown kinds are still checked |
| Added "irreversible" keywords | "Cancel Membership", "Unsubscribe", "Deactivate", "Confirm" and similar labels now need confirmation |
| Launch doesn't wait for the window | Keeps Tier 1 instant. Verifying the window appeared comes with UI Automation (Phase 9/11) |
| Fakes behind Protocols | Unit tests never change your volume or open apps; real checks are opt-in (`-m desktop`) |

## Measured on the dev machine

| Command | Route | Action |
|---|---|---|
| `volume 24` | 0.1 ms | 181 ms (incl. first COM call) |
| `find my resume` | 0.1 ms | 577 ms |
| App index build (191 apps) | — | ~2 s, once, then cached |

## How to test

```powershell
uv run pytest                    # 149 unit tests, no side effects
uv run pytest -m desktop         # real: opens/closes Notepad, sets & restores volume
uv run jarvis repl               # type commands; Esc stops a running task
uv run jarvis do "volume 30"
uv run jarvis apps spotify       # what does a name resolve to?
uv run jarvis apps --refresh     # rebuild the app index
```

## Limitations / next

- "play X on youtube" only opens the results page. Playing it needs the browser agent (Phase 5).
- "Launched" isn't verified, because there are no window checks yet.
- File search is bounded (3 folders, depth 5, 1.5 s) and doesn't use the Windows Search index yet.
- Brightness fails on external monitors without DDC/CI; this is reported clearly.
- Commands that don't match say "I can't do that yet"; the AI planner takes over in Phases 7, 11 and 14.
- Next is Phase 3: Piper TTS so Jarvis speaks while it acts.
