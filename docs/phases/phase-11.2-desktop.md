# Phase 11.2: General desktop agent (any Windows app)

**Goal:** the same agent that works on any website (Phase 11) now works in **any Windows app** —
Spotify, Settings, File Explorer, Calculator, Notepad, Word, WhatsApp desktop — by reading the
app's controls as text through **Windows UI Automation** (the accessibility tree screen readers
use) and using the mouse and keyboard like a person. **No app-specific code**: "play Blinding
Lights on Spotify" is just a task for it (old Phase 9 is gone).

Decisions taken with the user (2026-10-19): the `uiautomation` package (fast, thin) over
pywinauto; **Windows settings changes are confirmed** instead of handed over.

## How it works

```
"play blinding lights on spotify"
  rules: "… on spotify" is deliberately not matched (it would play on YouTube)
  LLM router (Haiku) → task, goal "play blinding lights on spotify", where: desktop
  "Working on it."
  TaskAgent.run(goal, "desktop")          — the SAME loop as websites, on the desktop Surface
    SEE    wait until the window stops changing → its controls, numbered:
             [7] combo box "What do you want to play?" value="jungkook sti"
             [21] button "Play Blinding Lights" …   + text, other open windows
    THINK  Haiku (desktop instructions + the same 11 rules) → ≤5 actions
    ACT    ActionRunner → SafetyPolicy → KindRouter → DesktopExecutor (args.surface = desktop)
    CHECK  something appeared / a dialog opened / the window changed → look again
  "Blinding Lights by The Weeknd is now playing on Spotify."
```

| Desktop action | How it's done (like a person) |
|---|---|
| `open_app {text}` | brings the app's window to the front if it's running (also when minimized), otherwise launches it with the existing app launcher and waits for its window |
| `switch {text}` | works in another open window |
| `click` | a real mouse click on what's on screen; check boxes / tabs / drop-downs use their own toggle / select / open; a button's "press" (Invoke) only when it can't be clicked |
| `double_click` | open a file or folder, play a song in a list |
| `type {element, text}` | a one-line box is replaced (Ctrl+A, type); in a document it's typed at the cursor (nothing deleted) |
| `type {text}` (no element) | keyboard typing where the cursor is — e.g. `234*47=` into Calculator |
| `press {key}` | key combos (Ctrl+L, Alt+Left, Enter …), sent as real key presses |
| `select`, `scroll`, `find`, `back`, `wait` | as on websites; `find` searches the whole window (also scrolled-away items) and scrolls the item into view |
| `check_file {text: full path}` | read-only: is the file really there (size, time)? If not, where are files with that name — so "saved on your Desktop" is checked, not believed |

## What was built

| File | Purpose |
|---|---|
| `src/jarvis/executors/desktop.py` | `UiaThread` (every UI Automation call on one thread — COM rules; the voice loop never blocks), `DesktopSession` (app windows; the working window; minimized apps restored; empty frames of closed Store apps ignored; settle; admin-prompt check), `DesktopExecutor` (the actions above), `PynputKeyboard` |
| `src/jarvis/tiers/structured/desktop_observer.py` | Window → `Snapshot`: buttons, fields, lists, menus, tabs, links, check boxes, rows …; only what's **inside the window** (modern apps keep scrolled-away items in the tree marked "on screen"); a **modal dialog** is listed alone and the note says what it covers; drop-downs show their selected value; password boxes flagged, never read; Store apps resolved to their real program (`systemsettings`) |
| `src/jarvis/tiers/structured/surface.py` | `Surface`: how the loop looks and notices change — `BrowserSurface`, `DesktopSurface` |
| `src/jarvis/tiers/structured/agent.py` | `BrowserAgent` → `TaskAgent` with surfaces; per-surface action lists; `value_change` flag; readable confirmations ("in Settings", current value); main planner retried once |
| `src/jarvis/tiers/structured/prompts.py` | `RULES` shared by `SYSTEM` (websites, byte-identical to Phase 11 — its prompt cache keeps working) and `DESKTOP_SYSTEM` (apps) |
| `src/jarvis/planner/schema.py`, `catalog.py` | Router decision `where: browser | desktop`; tasks can be in apps; Windows settings are tasks (confirmed), not "unsupported" |
| `src/jarvis/executors/router.py` | `KindRouter(surfaces=…)`: the same kinds go to the browser or desktop executor |
| `src/jarvis/safety/policy.py`, `config/safety.yaml` | `settings_apps` + `value_change` → confirm; `risky_keys` (Ctrl+Enter, Shift+Delete, …) → confirm; `change_system_setting` RED → YELLOW |
| `evals/apps/testapp.ps1` | A predictable Windows Forms app for tests: text boxes, drop-down, check box, password box, a 30-song list, a modal dialog |
| `tests/test_desktop_agent.py` | Logic tests (always) + live tests on the test app (`-m desktop`) |

## Safety (same path, new facts)

- Every step: `ActionRunner` → `SafetyPolicy`, no bypass. Control names carry the same keyword
  rules as web labels ("Delete account" → confirm).
- **Password boxes** (`IsPassword`) → handed to you; never read; typing at the cursor refuses if
  the cursor is in one.
- **Windows settings**: looking around is free; a step that changes a value is confirmed with its
  current value — real run: *select "Light" in "Choose your mode" (now "Dark") in Settings?*
  → "no" → "Okay, I didn't do it.", nothing changed.
- **Risky keys** in any app or site: Ctrl+Enter (send), Shift+Delete, Ctrl+Shift+Delete, Alt+S.
- **Administrator prompts** (UAC): the task stops — Jarvis never uses admin rights.
- The kill switch ignores the keys Jarvis itself presses (Escape in an app doesn't stop it).
- Confirmation questions no longer show internal details ("element=25, surface=desktop").

## What went wrong while building it (and the generic fixes)

| Problem | Why | Fix |
|---|---|---|
| Clicking "Preferences" froze the agent for 130 s | A button's UI Automation "press" (Invoke) waits until the dialog it opens is closed | Buttons get a real mouse click; Invoke only as a fallback |
| The dialog never showed up | UI Automation puts an app's dialog *inside* its main window's tree | Child windows that behave like windows are read as dialogs; Store apps' inner windows aren't |
| `find` didn't bring a song into view | Windows Forms list items can't "scroll into view" | Scroll the item's list step by step until it's visible |
| Calculator: 61 s, wrong digits | One click per button, the model mixed up numbers; and two waits per action | `type` without an element (keyboard); one wait per action, shorter quiet time → **6–17 s** |
| Spotify: 0 controls | Spotify was minimized (0×0 window) | Minimized apps are found and restored, and the agent waits until the window has a size |
| Settings: page cut off at 120 controls; Calculator listed inside Settings | Scrolled-away items reported "on screen"; all Store apps share one host process | Only controls inside the window are listed; no "same process = dialog" for Store apps; the real program is resolved |
| "Open Calculator" worked in an empty shell | A closed Store app leaves its empty frame window behind | Frames with nothing inside are ignored; a window that dies while being read counts as gone |
| A failed eval said "no Claude API key" | One OpenRouter hiccup; the message showed the last fallback's reason | The main planner is retried once; errors name the main planner's reason |
| Spoken answers ended in ".)" or "</br>" | Model debris | `tidy()` also drops unmatched ")" and tag-like leftovers |
| I overwrote the existing `tests/test_desktop.py` | My mistake while creating the new test file | Restored from git unchanged; the new tests live in `tests/test_desktop_agent.py` |

## The Word test: stay where the user is, and never claim an unchecked save

User: "make a new word file" → "write down the code for palindrome in python and save it in
desktop as jarvis". The first run **switched to Notepad** ("suits code better") and saved
`jarvis.txt`; a later run saved to **Documents** while saying "saved on your Desktop" — Word's small
"Save this file" box ignored the location, and the agent believed what it meant to do.

| Problem | Fix (generic) |
|---|---|
| The goal lost "the Word document from before" | Router: a command that carries on from the recent turns without naming an app or site gets it in the goal ("… in the Word document opened in the previous step") |
| The agent switched apps by its own preference | Rule 12: stay in the app / page the goal names or the previous steps used; switch only if asked, or if it truly can't be done there (and say so) |
| "Saved on your Desktop" when it wasn't | Rule 13: never claim more than you saw — after saving, **`check_file`** (new, read-only: is the file really there? if not, where are files with that name?) and report the real folder |
| `Desktop\jarvis` typed as a file name → a folder that doesn't exist | The snapshot lists **the user's real folders** ("Your folders: Desktop = C:\Users\…\OneDrive\Desktop …"); rule: type the full path; if a save box takes no path, open the full dialog (More options / Browse / Save As) |
| Word's full Save As dialog: the File name box never listed | More controls than fit (120): **fields and buttons first**, long lists of items (folders, files, rows) fill the rest |
| A dialog read while still opening showed 0 controls | An open window with no controls gets another look (≤ 1.5 s) |

Result (clean start): the code typed into the new Word document, `jarvis.docx` **on the Desktop**,
confirmed by `check_file` before saying so — 92 s / 13 steps (Word's small save box needed the
long way round: More options → This PC → Desktop; Phase 12 skills will replay the short way).

Privacy note: the names of open windows and tabs (e.g. Notepad tab names) are part of what the
cloud model sees, like the text of a web page; the contents of other tabs are not.

## Measured (2026-10-19, Claude Haiku 5.5 via OpenRouter)

Real apps on the user's PC (full path from the sentence, `jarvis` agent, Jarvis voice muted):

| Command | Result |
|---|---|
| "play blinding lights on spotify" | ✅ 22 s — replaced the old search, searched, clicked Play, checked the now-playing bar: "Blinding Lights by The Weeknd is now playing on Spotify." |
| "what is my display resolution in settings" | ✅ 9 s — "1920 by 1200, the recommended setting" |
| "open file explorer and tell me the newest file in my downloads" | ✅ 21 s — opened Downloads (double-click), read the Today group |
| "switch windows to light mode" (confirmation answered no) | ✅ 8 s — asked *select "Light" in "Choose your mode" (now "Dark") in Settings?*, did nothing |
| "switch windows to dark mode" (already dark) | ✅ "it's already set, so I didn't change anything" — no question needed |

`jarvis eval-agent` — **17/17 passed** (11 website tasks + 6 app tasks, incl. "stay in the current window"), avg 11.3 s, $0.0103 in
total:

| App task | Steps | Time | Result |
|---|---|---|---|
| fill a name, pick Nepal in a drop-down, save (test app) | 3 | 11.9 s | ✅ "Saved: Bhuvan, Nepal" |
| play a song that is below the visible part of a list | 3 | 12.7 s | ✅ found it, played it |
| turn on dark mode inside a modal Preferences dialog | 4 | 15.0 s | ✅ |
| delete my account → user says no | 1 | 3.2 s | ✅ nothing deleted |
| open Calculator and work out 234 × 47 | 2 | 5.0 s | ✅ "10,998" (61 s before typing + one-wait fixes) |

All 11 website tasks still pass with the shared loop (Phase 11 numbers unchanged within noise).

Router benchmark (`jarvis eval-router`, 83 cases incl. 5 new app cases): **98% correct / 2% wrong
actions** (Haiku) — all app cases (`where: desktop`) correct. "turn off the wifi" is now a desktop
task (a Windows setting, confirmed); "delete all my downloads" stays unsupported (bulk file
operations come with 11b).

Speed: one look at a window 0.25–0.5 s (Spotify: 85 controls in 0.36 s), waiting for it to settle
0.5–0.65 s.

## How to test

```powershell
uv run pytest tests/test_desktop_agent.py            # logic: routing, safety, keys, window matching
uv run pytest -m desktop tests/test_desktop_agent.py  # live, on the test app (moves the mouse, types)
uv run jarvis eval-agent --tasks app_form,app_play_from_long_list,app_dialog,app_delete_declined,calculator
uv run jarvis repl
#   play blinding lights on spotify
#   what is my display resolution in settings
#   open file explorer and tell me the newest file in my downloads
```

## Limitations / next

- The agent uses the **real mouse and keyboard**: don't type or move the mouse while it works
  (Esc stops it).
- Apps that draw everything themselves (games, some canvas apps) expose little to UI Automation —
  that's the vision fallback (Phase 15).
- One window at a time per step; multi-app tasks ("put this PDF's table in Excel and mail it")
  come with the decomposer (Phase 14).
- Big app trees are read up to 1,500 nodes per look; very dense apps may need `find`.
- Speed: app tasks take ~5–25 s; skills (Phase 12) will replay them in ~1–2 s.
- Next: **11.3 files in and out** — attach / upload a file on any website or app.
