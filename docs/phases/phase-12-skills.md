# Phase 12: Skills — doing a task the second time without thinking it through again

**Goal:** a new task goes through the agent (See → Think → Act → Check), which makes one model call
per round: 20–45 s for a form or a flight search. The second time the *same kind* of task comes in
("play Shape of You on Spotify" after "play Blinding Lights on Spotify"), Jarvis should repeat
what worked: no model call per step, and a few seconds instead of tens. This works on any site or
app, with no per-task code, and nothing is learned by hand.

Decision (user, 2026-10-21): skills are **learned automatically** from every run that worked.

## How it works

```
1st time  "send the contact form as Bhuvan from Nepal"
   router → task → agent (rounds of See → Think → Act → Check) → ok
   └─ in the background: the steps the agent took → Learner (one model call)
        → recipe "send the contact form as {person} from {country}"
          slots {person, country}, steps by what each control *is* (type + name, {slots} filled in)
        → checked: filling the slots back in must give exactly what was done → saved as JSON

2nd time  "send the contact form as Asha from India"
   router (it now sees the skills list) → task, skill = send_the_contact_form_…,
                                          slots {person: Asha, country: India}
   Replayer: for each step → look → find the control by type + name → the same to_action →
             the same ActionRunner / SafetyPolicy (Send is still confirmed) → next step
   end: the screen must show the recipe's check words
   ├─ ok → done (≈2–8 s, no model call after the router)
   └─ a step can't be done → the agent carries on *from the current screen*,
        told "a saved way stopped at step N: …"; if the agent succeeds, the skill is learned again
```

### Learning (`tiers/skills/learner.py`)
- **What's never learned:** runs with a password, code, card or UPI step, a credential or payment
  hand-off, or a CAPTCHA (`_NEVER` flags on any step); failed or cancelled runs; runs with nothing
  to repeat; and one-offs the model marks `worth_saving: false` (an answer read off a page).
- **One model call** (the same planner chain as the agent) turns the numbered steps into a recipe:
  the slots (values from the request that would change next time), the steps to keep (no retries
  or dead ends), check words and a done message.
- **The recipe is verified, not trusted.** Every kept step has a template with `{slot}`s. Filling
  the slots back in must give back exactly the name, text and address of the step the agent
  really took. Otherwise the recipe is dropped. A model can't invent a step or a slot that wasn't
  in the run.

### Replaying (`tiers/skills/replay.py`)
- Controls are stored by **what they are** (type + name), never by number or position, because
  numbers change every time the screen is read. Finding a control, most exact match first:
  1. the same name
  2. a name that contains it
  3. the same words in another order ("21 October 2026" ↔ "October 21, 2026")
  4. a name that contains all the slot values
  5. a fuzzy match (≥ 88)
- If the page is still drawing, it looks again for up to 2.5 s.
- **Typing goes where the cursor is** when the box changed its name after being clicked (a styled
  box that turns into a text field), the same as a person would.
- Every step goes through **the same `to_action` + ActionRunner + SafetyPolicy** as the agent. A
  replayed Send, Delete or Pay is confirmed exactly as in the first run, and a "no" stops the
  replay (it doesn't fall back to the agent).
- After the last step, the recipe's check words (slots filled) must be on the screen.

### Routing (`planner/router.py`, `planner/schema.py`)
- The router's system prompt ends with the skills list ("- id: name [where] — slots: …"). The
  list changes only when a skill is learned or dropped, so the prompt cache keeps working.
- The router picks a skill like a command: `kind: "task"`, `skill` = its id, and its `slots`. If
  slots are missing, it gets one retry with the problem explained. An unknown skill id is dropped,
  and the agent does the task.
- There is no regex or embedding match per skill: understanding stays with the model, as the
  no-hardcoding rule asks. (Phase 14 adds an embedding shortcut in front for speed.)

### Keeping skills healthy (`tiers/skills/store.py`)
- Each replay is counted (successes, failures, failures in a row, last used).
- **3 failed replays in a row → retired** (deleted). The next run goes through the agent and
  learns the task fresh, since sites change.
- Re-learning a skill with the same name replaces its steps and keeps its record.
- "What skills do you have" lists them. "Forget the spotify skill" deletes one, and asks which if
  several match. Both are green (nothing on screen changes).

## What was built

| File | Purpose |
|---|---|
| `src/jarvis/tiers/skills/store.py` | `Skill` / `SkillStep` models, `{slot}` filling, JSON store in `%LOCALAPPDATA%\JarvisAgent\skills\`, record / retire, find, the router's skills list |
| `src/jarvis/tiers/skills/learner.py` | never-learn checks, the learning prompt + `LearnedSkill` schema, `build()` verifies the recipe reproduces the run |
| `src/jarvis/tiers/skills/replay.py` | `find_control` (type + name, match order above), `Replayer.run` → `ReplayOutcome(ok, message, step, error, ms)` |
| `src/jarvis/tiers/skills/service.py` | `Skills`: learn in the background after a run that worked (`learn_later`), `settle()` on shutdown |
| `src/jarvis/executors/skills.py` | `list_skills` / `forget_skill` actions |
| `src/jarvis/tiers/structured/agent.py` | the agent records each step it took (`RunStep`: op, control type + name, text, key, address, ok, risk flags) in `last_run` / `last_where` |
| `src/jarvis/agent.py` | `_skill_or_agent`: replay → else / on failure the agent from the current screen → learn on success; skills wired into the router |
| `src/jarvis/planner/schema.py`, `router.py` | `skill` field; skills appended to the system prompt; skill slot validation with a retry |
| `config/intents.yaml`, `config/safety.yaml` | `list_skills`, `forget_skill` (green) |
| `src/jarvis/tiers/structured/evaluate.py`, `evals/agent_tasks.yaml` | eval tasks with an `again` variant: learn with the real model, route the variant with the real router, replay, check the page |
| `tests/test_skills.py` | recipe verification, never-learn secrets, fill / slug, find_control (reordered words), retire, focused-field typing, learn → replay with **zero** planner calls and Send confirmed both times, replay stops on a different page |

## Safety

Skills don't add a path around the safety layer. A replayed step is an ordinary action through
ActionRunner → SafetyPolicy, judged at execution time on the live screen (risk per step, never
per task). The things that are always the user's (passwords, OTP, card / UPI, CAPTCHAs) make a
run unlearnable, so a recipe never contains them. A recipe holds only control names, typed text
and addresses that were already visible on screen.

## Measured

`jarvis eval-agent` **36/38**. Both skill tasks pass. The two misses are the known flaky 11.4
cases: on checkout_confirmed, the model asked "should I place it?" itself instead of letting the
read-back do it; on asks_which_colour, it misread the stock. 551 tests pass; ruff and mypy are clean.

| Task | First time (agent) | Same kind again (replay) | Model calls in the replay |
|---|---|---|---|
| contact form (eval, test site) | 19–22 s | **2.5 s** | 0 |
| flight search: origin, destination, date (eval, test site) | 45–52 s | **7.8 s** | 0 |
| "play … on Spotify" (real app) | 13–24 s | **8–10 s** | 0 |

Routing takes another ~2.5–3.5 s each time (the router call that picks the skill).

Real Spotify run (2026-10-21):
- "play Believer on Spotify" was learned as: open Spotify → type `{song}` in "What do you want to
  play?" → Enter → click "Play {song}".
- "play Levitating", "play Shape of You" and "play Believer" were then replayed with no model call
  per step.
- Levitating's button is "Play Levitating (feat. DaBaby)". It was still found (the button contains
  the filled name).
- Most of a Spotify replay is reading the app's control tree (~1 s per look) and waiting for the
  search results; see refinement 5.

Found and fixed during the real runs:
- A step without a control was saved with the agent's own label as its "name". The replay then
  tried to open an app called "open_app"; the agent took over and finished. Now only a real control
  gives a name, and typing / keys at the cursor replay without looking for a control.
- A run that only worked because the song was already on screen (a recently played list) is no
  longer saved. Next time the song won't be there.

## How to run / test

```
uv run pytest tests/test_skills.py
uv run jarvis eval-agent                       # includes skill_form, skill_flights (learn → replay)
uv run jarvis do "play Blinding Lights on Spotify"   # agent; learns in the background
uv run jarvis do "play Shape of You on Spotify"      # replay
uv run jarvis do "what skills do you have"
uv run jarvis do "forget the spotify skill"
```
Skills live in `%LOCALAPPDATA%\JarvisAgent\skills\*.json`: readable, and deletable by hand.

## Limitations / next hooks
- **One path per skill.** A recipe follows the steps of the run it was learned from. A variant
  that needs a different path (a pop-up that wasn't there, an extra page) stops the replay, and
  the agent carries on from that screen.
- Learning costs one model call after each successful agent run (in the background, not waited on).
- Choosing a skill still costs the router call (~1.5 s). Phase 14 adds an embedding match
  (sqlite-vec) in front, so a known task can skip the model entirely.
- Skills are plain JSON for now. Phase 13 moves them into the encrypted database (SQLCipher + DPAPI).
- Vision clicks (`click_at` on a numbered box) aren't replayed, because box numbers don't carry
  over. A run that needed vision keeps its text steps, and a missing step falls back to the agent.
