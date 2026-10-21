# Phase 15: Vision — seeing what the text view can't

**Goal:** the agent reads pages and apps as text (fast, cheap). Some things only make sense to the
eye: controls that only *look* like buttons (Flipkart's "Buy now" — a styled box, no button, no
role, click handled by the site's script), options that are only pictures (colour swatches with
no names), icons, canvas apps and games. Vision lets the agent **look at the screen** when the
text isn't enough — on any site or app, with no per-site rules.

Decisions (user, 2026-10-21): **OmniParser on the GPU** finds the clickable things in the picture;
**Claude Haiku 5.5** (the same model, via OpenRouter) looks at it. Built before Phase 12 (skills)
because real shopping hit the text-view limit.

## How it works

```
text round (as before) — the agent can't find what it needs → action `look`
   (or: an app window that shows nothing readable)
vision round:
  1. screenshot of the working tab / window            in memory only, never saved
  2. OmniParser (YOLO, on the RTX 4050) → clickable boxes, numbered in reading order
  3. Haiku sees the picture with the numbers + the same goal / history / principles
     → "click box 2, label 'Buy now'"   (label = what the box shows, read from the picture)
  4. click at the centre of box 2 (browser: page coordinates; app: screen coordinates)
     → the same ActionRunner / SafetyPolicy: the label is what's judged
       ("Buy now" → order read-back + yes; no readable label → asked first)
next round: back to text (vision only when asked again; at most 3 looks per page)
```

## What was built

| File | Purpose |
|---|---|
| `src/jarvis/tiers/vision/detector.py` | OmniParser v2 `icon_detect` (YOLO): downloaded once (~40 MB) into the Jarvis data folder, loaded on first use, on the GPU (CPU fallback); duplicate boxes removed, reading order |
| `src/jarvis/tiers/vision/marks.py` | the screenshot with numbered boxes, as JPEG bytes (≤1600 px) |
| `src/jarvis/tiers/vision/eyes.py` | `Eyes.look(surface)` → screenshot → boxes → picture; `Look.click_point(n)` |
| `src/jarvis/tiers/structured/surface.py` | `screenshot()` for both surfaces (browser: the tab in CSS pixels; desktop: the window via mss) |
| `src/jarvis/tiers/structured/agent.py` | the `look` action; a vision round; `box_action` → `click_at` / `type_at`; look-only rounds aren't counted as loops (≤ 3 per page) |
| `src/jarvis/planner/llm.py` | every provider takes an optional picture (`decide(..., image=jpeg)`) |
| executors | `click_at` / `type_at` (browser: mouse at page coordinates, follows a new tab; desktop: screen click) |
| `config/safety.yaml`, `policy.py` | `click_at` labels go through the keyword rules; `label_required_kinds`: a screen click without a readable label → confirm; card / UPI fields → the user's |
| `pyproject.toml` | PyTorch 2.8 (CUDA 12.9, from PyTorch's index), ultralytics, mss, Pillow, huggingface-hub; cuBLAS pin relaxed to 12.9.1.4 (Whisper still on the GPU); Python 3.12–3.13 |
| tests / evals | `tests/test_vision.py` (fake box finder; the real detector under `-m desktop`); test pages `vision-buy.html` (a styled-box Buy now), `vision-swatch.html` (colours only), `vision-canvas.html` (a canvas app) |

## Safety and privacy

- Screenshots stay in memory: never written to disk or logs.
- Every vision click goes through the same policy as any other step; its **label** (what the box
  shows) is judged. A box the model can't name → confirmed first. "Buy now" / "Place order" →
  the order read-back.
- Page text in a screenshot is untrusted data, like on a text page.
- Licence: the detector weights are AGPL-3.0 — fine for a personal / resume project; a commercial
  product would swap the detector (only `detector.py` changes).

## Measured

`jarvis eval-agent` **34/36** (all three vision tasks pass; the two misses are the known flaky
"ask" / "should I?" cases from 11.4). 538 tests; ruff, mypy clean; Whisper still on the GPU.

| Vision task (test pages) | Result |
|---|---|
| "Buy now" is a plain styled box (text view lists nothing) | ✅ looked, found it, the safety check read the order back |
| colour swatches with no names — "choose the blue colour" | ✅ picked blue by looking |
| a canvas app — "pick Square" | ✅ |

Real flipkart.com (every question answered "no"): "order me apple iphone 17" → asked colour /
storage → "black one and 256gb" → product page; the add-to-cart control wasn't in the text view,
so it **looked** and clicked the cart button → **the iPhone was in the cart** → "Place Order" in
the cart was stopped by the safety check (no address / payment on that page) → it asked for a
delivery pincode, because Flipkart said the phone isn't deliverable to the profile's current
pincode. Nothing was bought.

Note for later: on some shops "Place Order" in the *cart* only opens checkout; the label-based
gate refuses it when the order details aren't shown, which can block the normal way in. A later
option: confirm ("this may place the order — continue?") instead of refusing on such pages.

## The agent keeps a plan (added with this phase)

User (2026-10-21): "the agent should think what it must do to reach the goal" — e.g. notice it
needs a pincode and ask. Every step the model now also writes a short **plan** (steps to the goal,
marked done / now / next) and the current **blocker**, and both are shown back to it next step
("Your plan so far …", "Blocker you noted last time …"). Principle 2: solve a blocker from the
screen when possible, otherwise ask one short question. No site rules; logged in the traces.

Real flipkart.com afterwards (every question answered "no"): from the cart it went through
checkout (address already selected: Vellore), past an "open-box delivery" pop-up, to the payment
page — total ₹89,298 (₹88,999 + ₹299 protect fee) — and asked **which payment method**, since none
was saved / selected and Cash on Delivery wasn't offered. Nothing was bought. `eval-agent`
33/36 (the misses are the known flaky ask / colour-picture cases).

## How to test

```powershell
uv run pytest tests/test_vision.py
uv run pytest -m desktop tests/test_vision.py   # the real OmniParser detector on the GPU
uv run jarvis eval-agent --tasks vision_styled_buy_button,vision_colour_swatch,vision_canvas
```

## Limitations / next

- A vision round takes ~5–10 s (screenshot, detector, a picture to the model) — used only when
  the text view isn't enough.
- The model decides *when* to look; it doesn't yet look on its own after repeated "not found".
- Desktop screenshots assume the window is visible and not covered by another one.
- Next: Phase 12 (skills) — replay what worked (vision runs included) in ~1–3 s.
