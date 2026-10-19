# Phase 5: Browser control (Chrome over CDP)

**Goal:** Jarvis drives a real, visible Chrome window: "play lofi on YouTube" actually plays the
video, the current video and tab can be controlled by voice, a "verify you are human" check pauses
the task and hands control to you, and actions on checkout/payment pages get a higher risk.

## The Jarvis Chrome window (why it's separate)

Chrome 136+ refuses remote control (`--remote-debugging-port`) of its **default data folder**,
`%LOCALAPPDATA%\Google\Chrome\User Data`. That one folder holds *all* your normal profiles
(`Default`, `Profile 1`, …), so none of them can be driven — this blocks malware from hijacking your
logged-in sessions. Jarvis therefore starts Chrome with its own data folder:

```
%LOCALAPPDATA%\JarvisAgent\chrome-profile      ← chrome.exe --user-data-dir=… --remote-debugging-port=9222
```

It's the same Chrome program in a normal, visible window, but a separate world: it isn't in your
profile picker, shares no tabs/cookies/history with your Chrome, and starts with no logins.
`jarvis browser setup` opens it **without** remote control so you can sign in (Google blocks sign-in
in controlled browsers); logins are then remembered. Copying your existing logins in isn't possible
(Chrome's app-bound cookie encryption) and isn't attempted.

## What you can say

| Say | What happens |
|---|---|
| "play lofi on YouTube" / "… in YouTube" / "… on yt" | search → first real video → plays → "Playing: …" |
| "play a hardstyle music" / "put on some lofi" (no app named) | YouTube for now (Spotify arrives in Phase 9; "… on spotify" is left unmatched, never played on YouTube) |
| "search lofi on YouTube" | opens the results page only |
| "pause" · "resume" / "play" · "next" / "skip" · "full screen" | the current YouTube video |
| "fast forward by 10 min" · "skip 30 seconds" · "rewind" · "go back 1 minute" | seek (bare "fast forward"/"rewind" = 10 s) → "At 10:00 of 1:01:13" |
| "go to 5:30" · "jump to 10 minutes" · "start over" | jump to a time / restart |
| "speed 1.5x" · "faster" · "slower" · "normal speed" | playback speed (0.25–2x) |
| "skip the ad" | clicks Skip if an ad can be skipped |
| "always skip ads" · "never skip ads" · "ask me about ads" | ad setting for this session |
| "go back" · "go forward" · "refresh" · "new tab" · "close this tab" | the current tab |
| "open instagram" / "go to github.com" | opens in the Jarvis Chrome window (new tab, or the empty one) |

**Ads (default: ask).** When an ad's Skip button appears, Jarvis mutes the ad (so the mic hears you,
not the ad) and asks "There's an ad. Skip it?". Yes → skip. No, or no answer in 8 s → it plays.
An ad that can't be skipped is announced once. Default in `config/apps/youtube.yaml` (`ads: ask | skip | play`).

## What was built

| File | Purpose |
|---|---|
| `src/jarvis/executors/browser_session.py` | `BrowserSession`: find Chrome, launch it detached with the Jarvis data folder + CDP port, `connect_over_cdp`, reconnect; `pick_active()` (the tab you're looking at); helpers used by `jarvis browser` |
| `src/jarvis/executors/browser.py` | `BrowserExecutor`: `open_url`, `youtube_play`, `media_key`, `browser_nav`, `set_ad_policy`; `annotate()` adds the current tab's URL before the safety check; if Chrome can't be started it fails with a clear message (never a silent switch to another browser) |
| `src/jarvis/executors/router.py` | `KindRouter`: one executor that routes by action kind, so `ActionRunner` still has a single path |
| `src/jarvis/apps/youtube_flow.py` + `config/apps/youtube.yaml` | Search URL, `pick_video()` (skips ads, Shorts, live), click, wait-until-playing, ad handling; all selectors in YAML |
| `src/jarvis/safety/captcha.py` | `CaptchaGate`: detect challenge iframes/phrases → handoff through `ActionRunner` (RED) → resume when it disappears |
| `src/jarvis/safety/policy.py`, `config/safety.yaml` | `url_rules` (checkout paths, payment-gateway hosts → PAYMENT), `url_kinds`, `captcha` rules; `Action.page_url` |
| `src/jarvis/executors/base.py` | `ActionRunner(annotate=…)`: context added before the policy runs |
| `src/jarvis/safety/confirm.py` | `can_cancel()` (a terminal prompt can't be abandoned); handoff message in plain words |
| `src/jarvis/executors/os_executor.py` | "open X" that resolves to a website opens in the Jarvis Chrome |
| `src/jarvis/agent.py` | Wires it all; `prewarm(launch_browser=…)`, `close()` |
| `src/jarvis/main.py` | `jarvis browser setup` / `jarvis browser status` |
| `config/intents.yaml` | `youtube_play`, media and tab intents (placed before the generic open/close intents) |
| tests | `test_browser.py` (headless Chromium on `tests/fixtures/browser/*.html`), `test_browser_units.py`, desktop test on real Chrome + YouTube |

New dependency: `playwright`. Tests also use its bundled Chromium (`uv run playwright install chromium`).

## How it works

```
"play lofi on youtube" → Intent(youtube_play) → Action(kind=youtube_play, target="lofi")
  ActionRunner: annotate (page context) → policy (GREEN) → KindRouter → BrowserExecutor
    BrowserSession.pages()  connect over CDP (launch the Jarvis Chrome if needed, ~1–2 s once)
    YouTube tab (reuse) → goto results?search_query=lofi → CAPTCHA check
    pick_video: first ytd-video-renderer not inside an ad/Shorts shelf, not /shorts/ or /live, no LIVE
                badge, and WITH a duration label (live streams have none); retried while labels
                render, relaxed only as a last resort
    click → poll player state every 250 ms:
        ad showing  → Skip visible? ask / skip / leave · unskippable → announce once
        no ad       → <video> playing? → "Playing: <title>"   (nudge play() once after 5 s)
```

**CAPTCHA handoff.** After each navigation `CaptchaGate.check(page)` looks for reCAPTCHA / hCaptcha /
Cloudflare / Arkose iframes and phrases like "verify you are human". If found: the window comes to
the front and an `Action(kind="solve_captcha", flags={"captcha_detected"})` goes through
`ActionRunner` → RED → spoken handoff ("…say done when you've finished"). Jarvis resumes when you
say "done" **or** as soon as the challenge disappears (voice mode). It never tries to solve it.

**Page-context risk.** For `media_key`/`browser_nav`, `annotate()` puts the current tab's URL in
`action.page_url`; for `open_url` the target URL itself is checked. Checkout paths (`/checkout`,
`/gp/buy`, …) and payment gateways (Razorpay, PayU, Paytm, CCAvenue, BillDesk, Stripe Checkout, …)
raise the action to PAYMENT — which, without full payment details, is refused.

## Design decisions

| Decision | Why |
|---|---|
| Separate Chrome data folder | The only thing Chrome 136+ allows; also keeps your real sessions out of reach |
| Chrome launched detached | Survives Jarvis restarts; reconnecting takes ~0.3 s |
| URL shortcut to results | No typing into the search box: faster and more reliable |
| Seek/speed by setting `video.currentTime` / `playbackRate` | Exact ("10 min" means 600 s), ~10–40 ms, clamped to the video; refused on live streams. Seek intents sit before next/go-back so "skip 30 seconds" and "go back 1 minute" mean seeking |
| JS `video.pause()/play()` for pause/resume | YouTube's `k` toggles — the wrong state would flip it |
| Shortcuts for next/fullscreen | Keyboard over mouse; they're YouTube's own |
| Require a duration label | Real 24/7 streams ("lofi hip hop radio") showed no LIVE badge — found in the user's first test |
| Retry the Skip click | A click can miss while the button re-renders; otherwise you'd sit through the ad |
| Recognise YouTube error pages (`error_pages`) | In a real test YouTube redirected to `/oops` ("error … creating a YouTube account") and Jarvis waited 14 s, then said "took too long". Now it says what's wrong within ~2 s |
| Report ad time ("after 38 s of ads") | Long waits are almost always unskippable ads; now it's visible, not a mystery |
| Don't filter `&list=` | YouTube adds its auto "radio" queue to ordinary video links (found on the real site) |
| Mute the ad while asking | Otherwise the open mic transcribes the ad as your answer |
| Websites only in the Jarvis Chrome window | Your rule: no website ever opens in your normal profile or in Edge; if Chrome can't start you're told, not silently switched |
| Handoff through `ActionRunner` | Same logging, speaking and confirmer as every other RED step — no special path |
| Resume when the CAPTCHA disappears | You shouldn't have to say "done" after solving it |
| Headless Chromium + local fake pages in tests | Real browser behaviour, no network, deterministic |

## Measured on the dev machine

| | Time |
|---|---|
| Launch Jarvis Chrome + connect (cold) | ~1–2 s, once |
| Connect to an already-open Jarvis Chrome | ~0.3–0.4 s (in the background at startup) |
| "play arijit singh songs on YouTube" → playing | 3.4 s (page load dominates) |
| pause / resume / next | 16–49 ms |
| "open instagram" | 0.6 s |
| back / close tab | ~25 ms |
| Voice mode startup (Whisper + browser connect in parallel) | 5.2 s |

## How to test

```powershell
uv run playwright install chromium          # once, for the browser tests
uv run pytest                                # includes headless browser tests on local pages
uv run pytest -m desktop -k real_chrome      # real Chrome + real YouTube: plays, then pauses
uv run jarvis browser setup                  # once: sign in inside the Jarvis Chrome window
uv run jarvis browser status
uv run jarvis run                            # "Hey Jarvis, play lofi on YouTube" … "pause"
```

## Closing the Jarvis Chrome window (fix from user testing)

If you close the window (or its tab) and give a browser command, Jarvis notices the page is gone
(Playwright's "Target page, context or browser has been closed"), drops the stale connection
(`BrowserSession.reset()`), reopens the window and does the command once more. A second failure gives
a plain "couldn't be reopened" message. Closed tabs are never picked, and browser errors log one line
(the traceback only with debug logging). Measured: close window → "play lofi on youtube" → reopened and
playing in 5.6 s.

## Limitations / next

- YouTube account problems in the Jarvis Chrome window (seen: `youtube.com/oops`, "An error occurred
  while attempting to create a YouTube account" — typical for school/work Google accounts) are
  YouTube-side. Jarvis now names them (also sign-in and cookie-consent redirects, from
  `error_pages` in `youtube.yaml`); the fix is to use a personal Gmail in that window, or sign out.
- "play X's **latest** video" just searches that text — it doesn't know what "latest" means or open
  the channel's Videos tab. That's Phase 7 + 11 (user's choice: no YouTube-specific shortcut).

- Unskippable ads (often two in a row) can add 15–45 s before the video itself starts; the command
  waits for the real video (say "stop" to cancel). The result and the log show how long ads took.
- In typed mode (`jarvis repl`) the "Skip it?" question waits for your answer (a terminal prompt
  can't time out); in voice mode no answer within 8 s means "no".

- The Jarvis Chrome window has no logins until you run `jarvis browser setup`.
- While it runs, the control port (127.0.0.1:9222) is reachable by other programs on this PC — the
  same trade-off any CDP tool has. It's a separate data folder, so your normal Chrome sessions are
  never exposed.
- **Every website opens in the Jarvis Chrome window** — including installed browser web apps
  (Chrome/Edge PWAs such as YouTube, Gmail, Docs, Drive, Sheets, Slides). Those run in your normal
  profile (`--profile-directory=Default`), where Jarvis could open but never control them, so
  `AppEntry.is_web_app` makes the resolver open their site instead (even for "open youtube app").
  A web app with no address in `aliases.yaml` `sites` is not opened at all — add its site there.
  Real desktop apps (WhatsApp, Spotify, Chrome itself) are unchanged.
- "First real video" is not always the *best* match; choosing smartly is an LLM job (Phase 7).
- YouTube changes its page often; selectors live in `config/apps/youtube.yaml`. Tier 3 (Phase 11)
  will read the page itself when selectors break.
- Next: Phase 7 (LLM layer) before Phase 6 (Amazon), since Amazon needs the LLM to pick a product.
