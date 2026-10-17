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
            risk of action.flags )           # e.g. password_field, captcha_detected
```

- **Risk only goes up.** A harmless kind cannot lower a risky label or flag.
- **Keywords are matched as whole words, case-insensitively.** "Send" matches; "Sender settings" does not.
- **Keywords are checked against `target` only, never typed text.** Dictating "send the password"
  into Word is not treated as a risky action.
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

Voice and on-screen confirmers will implement the same protocol in later phases.

## Kill switch (`safety/killswitch.py`)

- `await KillSwitch("esc").guard(task_coroutine)` runs the task. Pressing **Esc** cancels it and raises `Killed`.
- The global hook (pynput) is active **only while a task runs**, so normal Esc use is unaffected.
- Keys the agent itself sends are flagged by Windows as *injected* and ignored, so the agent can't kill itself.
- The key comes from `config/settings.yaml` → `hotkeys.kill`.

## Redaction (`safety/redact.py`)

`redact(text)` masks the following. `RedactingFilter` is attached to every log handler by `setup_logging()`.

| Data | Rule | Output |
|---|---|---|
| Card number | 13–19 digits that pass the Luhn checksum | `[CARD]` |
| OTP / PIN / UPI PIN / CVV | Digits right after those words | `OTP is [REDACTED]` |
| Password | Value after `password:` / `password is` | `password: [REDACTED]` |
| Aadhaar | Exactly 12 digits (not part of a longer number) | `[AADHAAR]` |
| PAN | `ABCDE1234F` pattern | `[PAN]` |

Emails and phone numbers are **kept** on purpose, because the agent needs them for contacts and read-backs.

## Try it

```powershell
uv run jarvis demo-safety
```
