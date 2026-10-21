"""Phase 12: skills — learn a recipe from a run that worked, replay a variant with no model
calls."""

from __future__ import annotations

from typing import Any

from playwright.async_api import Browser
from test_agent_loop import ScriptPlanner, browser, el, make, site, step  # noqa: F401

from jarvis.planner.llm import LLMReply, Usage
from jarvis.tiers.skills.learner import (
    LearnedSkill,
    LearnedStep,
    Learner,
    SlotDef,
    build,
    learnable,
)
from jarvis.tiers.skills.replay import Replayer, find_control
from jarvis.tiers.skills.store import Skill, SkillStep, SkillStore, fill, slug
from jarvis.tiers.structured.agent import RunStep
from jarvis.tiers.structured.observer import Element, Snapshot
from jarvis.tiers.structured.schema import AgentAction
from jarvis.tiers.structured.surface import BrowserSurface

RUN = [
    RunStep(op="type", tag="input", name="Your name", text="Bhuvan"),
    RunStep(op="select", tag="select", name="Country", text="Nepal"),
    RunStep(op="click", tag="input", name="Send me news", ok=False),  # a dead end
    RunStep(op="click", tag="button", name="Send message", flags=["agent_irreversible"]),
]
LEARNED = LearnedSkill(
    worth_saving=True, name="send the contact form as {person} from {country}",
    description="fills and sends the shop's contact form",
    slots=[SlotDef(name="person", value="Bhuvan", about="the name"),
           SlotDef(name="country", value="Nepal", about="the country")],
    steps=[LearnedStep(step=1, text="{person}"), LearnedStep(step=2, text="{country}"),
           LearnedStep(step=4)],
    check="Got: {person} from {country}", done_message="Sent the form as {person}.",
)  # fmt: skip


class FakeModel:
    name, model = "openrouter", "fake"

    def __init__(self, reply: Any) -> None:
        self.reply = reply
        self.calls = 0

    async def decide(self, system: str, user: str, schema: Any = None, max_tokens: int = 0,
                     image: bytes | None = None) -> LLMReply:  # fmt: skip
        self.calls += 1
        return LLMReply(self.reply, self.name, self.model, 1.0, Usage())


# ---- recipe -----------------------------------------------------------------------------------


def test_a_recipe_is_checked_against_what_was_done() -> None:
    skill = build(LEARNED, RUN, "browser")
    assert skill is not None and skill.id == "send_the_contact_form_as_person_from_country"
    assert [s.text or s.name for s in skill.steps] == ["{person}", "{country}", "Send message"]
    wrong = LEARNED.model_copy(update={"steps": [LearnedStep(step=1, text="{country}")]})
    assert build(wrong, RUN, "browser") is None  # "{country}" ≠ what was typed ("Bhuvan")


def test_runs_with_secrets_or_handoffs_are_never_learned() -> None:
    assert learnable([RunStep(op="type", name="Password", text="x", flags=["password_field"])])
    assert learnable([]) and learnable(RUN) is None


def test_slots_fill_and_names_slug() -> None:
    assert fill("Play {song} now", {"song": "Shape of You"}) == "Play Shape of You now"
    assert slug("play {song} on Spotify") == "play_song_on_spotify"


def test_controls_are_found_by_what_they_are() -> None:
    snap = Snapshot(url="u", title="t", elements=[
        Element(id=1, tag="button", name="Play Shape of You"), Element(id=2, tag="a", name="Home"),
        Element(id=3, tag="button", name="Play Shape of You (Live)")])  # fmt: skip
    hit = find_control(snap, "button", "Play Shape of You", {"song": "Shape of You"})
    assert hit is not None and hit.id == 1
    assert find_control(snap, "button", "Play Perfect", {"song": "Perfect"}) is None


def test_the_store_retires_a_skill_that_keeps_failing(tmp_path: Any) -> None:
    store = SkillStore(tmp_path)
    store.save(Skill(id="s", name="do {x}", steps=[SkillStep(op="wait")]))
    assert "- s: do {x}" in store.catalog()
    assert not store.record("s", False) and not store.record("s", False)
    assert store.record("s", False) and store.get("s") is None  # 3 in a row → gone
    assert not (tmp_path / "s.json").exists()


# ---- learn from a real run, then replay a variant ---------------------------------------------


async def test_learn_then_replay_a_variant_without_the_model(
    browser: Browser,  # noqa: F811
    tmp_path: Any,
) -> None:
    planner = ScriptPlanner(
        lambda p: step("continue", None,
                       AgentAction(op="type", element=el(p, "Your name"), text="Bhuvan"),
                       AgentAction(op="select", element=el(p, "Country"), text="Nepal")),
        lambda p: step("continue", None,
                       AgentAction(op="click", element=el(p, "Send message"),
                                   risk_hint="irreversible")),
        lambda p: step("done", "Sent."),
    )  # fmt: skip
    agent, page, confirmer = await make(browser, "form.html", planner, answers=[True, True])
    assert (await agent.run("send the contact form as Bhuvan from Nepal")).ok

    model = FakeModel(LEARNED.model_copy(update={"steps": [
        LearnedStep(step=1, text="{person}"), LearnedStep(step=2, text="{country}"),
        LearnedStep(step=3)]}))  # fmt: skip
    skill = await Learner([model]).learn("send the contact form as Bhuvan from Nepal",
                                         agent.last_run, "browser")  # fmt: skip
    assert skill is not None
    store = SkillStore(tmp_path)
    store.save(skill)

    # a variant, replayed: no planner calls; the Send still asks the user (yes)
    await page.goto(site("form.html"))
    asked_before = len(planner.prompts)
    from jarvis.config import AgentSettings

    replay = await Replayer(agent.run_action, AgentSettings()).run(
        skill,
        {"person": "Arjun", "country": "Sri Lanka"},
        BrowserSurface(agent.surfaces["browser"].browser),  # type: ignore[attr-defined]
    )
    assert replay.ok, replay.message
    assert replay.message == "Sent the form as Arjun."
    assert "Got: Arjun from Sri Lanka" in await page.inner_text("body")
    assert len(planner.prompts) == asked_before  # the model was never asked
    assert len(confirmer.prompts) == 2  # the Send was confirmed again on replay


async def test_replay_stops_where_the_page_differs(browser: Browser, tmp_path: Any) -> None:  # noqa: F811
    agent, page, _ = await make(browser, "home.html", ScriptPlanner())
    skill = build(LEARNED, RUN, "browser")
    assert skill is not None
    from jarvis.config import AgentSettings

    replay = await Replayer(agent.run_action, AgentSettings()).run(
        skill, {"person": "Arjun", "country": "Nepal"}, agent.surfaces["browser"]
    )
    assert not replay.ok and replay.step == 1 and replay.error == "not_found"


def test_typing_goes_where_the_cursor_is_when_the_box_changed() -> None:
    from jarvis.tiers.skills.replay import find_control

    snap = Snapshot(url="u", title="t", elements=[
        Element(id=1, tag="input", name="Departure From", focused=True)])  # fmt: skip
    # the recorded name ("… inputbox") is gone: find_control finds nothing, replay uses the focus
    assert find_control(snap, "div", "Departure From New Delhi inputbox", {}) is None


def test_the_same_words_in_another_order_match() -> None:
    from jarvis.tiers.skills.replay import find_control

    snap = Snapshot(
        url="u", title="t", elements=[Element(id=4, tag="div", name="Choose October 30, 2026")]
    )
    hit = find_control(snap, "div", "Choose 30 October 2026", {"date": "30 October 2026"})
    assert hit is not None and hit.id == 4


async def test_steps_without_a_control_replay_at_the_cursor() -> None:
    # open an app / press a key / type where the cursor is: no control was used in the run, so
    # replay doesn't look for one (a step's own label is never taken for a control name)
    from jarvis.config import AgentSettings
    from jarvis.tiers.skills.replay import Replayer
    from jarvis.tiers.skills.store import SkillStep

    async def never(action: Any) -> Any:
        raise AssertionError("not run here")

    replayer = Replayer(never, AgentSettings())
    snap = Snapshot(url="app://spotify", title="Spotify", elements=[])
    for recipe_step, want in (
        (SkillStep(op="open_app", text="Spotify"), ("open_app", "Spotify", None)),
        (SkillStep(op="press", text="Ctrl+K", key="Ctrl+K"), ("press", "Ctrl+K", "Ctrl+K")),
        (SkillStep(op="type", text="{song}"), ("type", "Shape of You", None)),
    ):
        act, why = await replayer._step_action(recipe_step, {"song": "Shape of You"}, snap, None)  # type: ignore[arg-type]
        assert act is not None and why is None
        assert (act[0].op, act[0].text, act[0].key) == want and act[0].element is None


def test_a_step_without_a_value_gets_no_template() -> None:
    # the model may give a control-less step a "name" (e.g. its op); the recipe keeps none
    steps = [RunStep(op="open_app", tag="", name="", text="Spotify", key="", url="", ok=True,
                     kind="launch_app", flags=[])]  # fmt: skip
    learned = LearnedSkill(worth_saving=True, name="open {app}", description="",
                           slots=[], steps=[LearnedStep(step=1, name="open_app")])  # fmt: skip
    skill = build(learned, steps, "desktop")
    assert skill is not None and skill.steps[0].name == "" and skill.steps[0].text == "Spotify"
