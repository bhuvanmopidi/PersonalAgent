# Safety layer

Code: `src/jarvis/safety/` and `src/jarvis/executors/base.py`. Rules: `config/safety.yaml`.

## Risk levels and decisions

| Level | Examples | Decision | What happens |
|---|---|---|---|
| 🟢 GREEN | open app, open URL, search, play, type text | **ALLOW** | Runs immediately |
| 🟡 YELLOW | send, submit form, delete/move file, place order, **any unknown action** | **CONFIRM** | Reads back the details and runs only on an explicit "yes" |
| 💳 PAYMENT | pay, or clicking "Proceed to Pay" / "Pay now" | **CONFIRM** or **REFUSE** | All five details (item, quantity, price, address, payment method) must be present, or it is refused without asking |
| 🔴 RED | OTP, UPI PIN, password, login, CAPTCHA, system settings | **HANDOFF** | The user does the step; the agent's executor is never called |

## How the risk is computed

```
risk = max( risk of action.kind,             # unknown kind → YELLOW
            risk of keywords in action.target,  # the UI label, e.g. "Delete forever"
            risk of action.flags,            # e.g. password_field, captcha_detected
            risk of the page / URL )         # action.page_url, or the target for open_url
```

- **Risk only goes up.** A harmless kind cannot lower a risky label or flag.
- **Keywords are matched as whole words, case-insensitively.** "Send" matches; "Sender settings" does not.
- **Keywords are checked against `target` only, never typed text.** Dictating "send the password"
  into Word is not treated as a risky action.
- **Keywords apply only to kinds whose target is a UI label** (`keyword_kinds`: click, hotkey,
  press_key, submit_form), **plus any unknown kind**. For `open_url` the target is a URL built from
  your own words, so "google how to delete my account" stays GREEN.
- "Irreversible" labels are YELLOW: cancel membership/subscription, unsubscribe, deactivate,
  close/delete account, transfer, confirm, uninstall, reset, permanently.
- Every decision records its `reasons`, for example `["kind 'click' is GREEN", "target matches 'pay now' (PAYMENT)"]`.
  These are logged on every action.

## Tuning the rules

Edit `config/safety.yaml`. No code change is needed:

```yaml
kinds:     { green: [...], yellow: [...], payment: [...], red: [...] }
keywords:  { yellow: [send, delete, ...], payment: [pay now, ...], red: [password, otp, ...] }
flags:     { red: [password_field, otp_field, captcha_detected] }
payment_required_fields: [item, quantity, price, address, payment_method]
```

## One-tap checkout

The agent may click **Pay** only after you confirm a prompt like:

> Pay Rs 1,299 for boAt Airdopes 141 (qty 1), deliver to Home, using saved HDFC card?

It never stores card numbers, CVV or UPI PIN. OTP, UPI PIN and 3-D Secure are always entered by you
(they are RED). Using only payment methods already saved on the site is enforced in the shopping
flows (Phase 6).

## Confirmations (`safety/confirm.py`)

| Class | Use |
|---|---|
| `Confirmer` (Protocol) | `confirm(prompt) -> bool`, `handoff(message) -> bool` |
| `TerminalConfirmer` | Asks in the terminal without blocking the event loop (`asyncio.to_thread`) |
| `ScriptedConfirmer` | Pre-set answers for tests and evals; records every prompt |
| `SpokenConfirmer` | Speaks the prompt, then asks the inner confirmer (Phase 3) |
| `VoiceConfirmer` (`voice/session.py`) | Takes the answer from your next utterance (Phase 4) |

**Voice answers default to NO.** `parse_yes_no` needs a clear yes or no; a reply with both ("don't
send it") or neither is unclear. Unclear → asked once more → still unclear, or 20 s of silence →
cancelled. Handoffs (OTP, login, CAPTCHA) wait up to 10 minutes for "done" or "cancel". While
Jarvis reads a prompt aloud the mic ignores open-mode speech, so it can't answer itself. An
on-screen confirmer comes with the Tauri UI.

## Kill switch (`safety/killswitch.py`)

- `await KillSwitch("esc").guard(task_coroutine)` runs the task. Pressing **Esc** cancels it and raises `Killed`.
- The global hook (pynput) is active **only while a task runs**, so normal Esc use is unaffected.
- Keys the agent itself sends are flagged by Windows as *injected* and ignored, so the agent can't kill itself.
- The key comes from `config/settings.yaml` → `hotkeys.kill`.
- Saying **"stop"** (or "cancel", "never mind"…) while a task runs triggers the same switch; the
  result reads `stopped by voice`. No wake word is needed while a task runs.

## Redaction (`safety/redact.py`)

`redact(text)` masks the following. `RedactingFilter` is attached to every log handler by `setup_logging()`.

| Data | Rule | Output |
|---|---|---|
| Card number | 13–19 digits that pass the Luhn checksum | `[CARD]` |
| OTP / PIN / UPI PIN / CVV | Digits right after those words | `OTP is [REDACTED]` |
| Password | Value after `password:` / `password is` | `password: [REDACTED]` |
| Aadhaar | Exactly 12 digits (not part of a longer number) | `[AADHAAR]` |
| PAN | `ABCDE1234F` pattern | `[PAN]` |

The same `redact()` runs on everything Jarvis **speaks** (Phase 3), so secrets are never read aloud.

Emails and phone numbers are **kept** on purpose, because the agent needs them for contacts and read-backs.

## Page context (`url_rules`, Phase 5)

Browser actions carry `page_url`, filled in by `ActionRunner`'s annotator (the current tab) before
the policy runs; for `open_url` (`url_kinds`) the target URL itself is checked. Matching
`url_rules.payment` (checkout paths like `/checkout`, `/gp/buy`; gateway hosts like razorpay.com,
payu.in, paytm.com, checkout.stripe.com, matched as the host or a subdomain) makes the action at
least PAYMENT, so without full payment details it is **refused**, even a "pause".

## CAPTCHA handoff (`safety/captcha.py`, Phase 5)

After every page load, `CaptchaGate` checks for challenge iframes (`captcha.iframe_hosts`:
reCAPTCHA, hCaptcha, Cloudflare, Arkose) and phrases (`captcha.phrases`: "verify you are human",
"unusual traffic from your computer", …). If found, the window comes to the front and a
`solve_captcha` action with flag `captcha_detected` goes through `ActionRunner` → RED → handoff.
Only a challenge frame you can **see** counts (at least 20×20 px, not hidden): many sites preload
Google's invisible reCAPTCHA helper (0×0, `display:none`) on every page — on yatra.com it stopped a
finished flight search with a false "human check". A real challenge is always visible (you have
to click it), and the phrase check and the RED label keywords are unchanged.
The task resumes when you say "done", or as soon as the challenge disappears. **It is never solved
by the agent.** In typed mode it waits for Enter (a terminal prompt can't be abandoned; see
`can_cancel()`).

## The general agent's steps (Phase 11)

Every step the browser agent takes is an ordinary `Action` through `ActionRunner`:
- the target is the clicked/typed element's **visible label** → the keyword rules apply on any site;
- `page_url` → the checkout/payment URL rules; `navigate` targets are URL-checked too;
- password / one-time-code fields (from the page structure) → `password_field` / `otp_field` → handoff;
- **AI risk hint (raise-only):** the planner tags steps `irreversible` / `payment` / `credentials` →
  flags `agent_irreversible` (confirm), `agent_payment` (payment rules), `agent_credentials` (handoff);
- `press_key` is judged by the focused element's label (Enter on a "Send" button asks first);
- a "no" ends the whole task; page text is treated as untrusted data in the planner's prompt
  (prompt-injection guard — tested with an injected "delete the account" page).

## Planned layers

Each one can only *raise* risk:

| Layer | Phase |
|---|---|
| Per-app risky shortcuts (e.g. Gmail Ctrl+Enter = send) | 11.2 |
| Shell commands: PowerShell AST, verb semantics, allowlist, obfuscation → refuse | 11b |
| Learned risk classifier (≥99% recall on PAYMENT/RED) | 14b |
| Adversarial safety evals (miss rate per layer) | 16 |

## Try it

```powershell
uv run jarvis demo-safety
```
