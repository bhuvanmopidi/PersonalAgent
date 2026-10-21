"""The general browser agent (Phase 11) on local test sites, with a scripted planner.

The planner is scripted (no network); everything else is real: headless Chromium, the observer,
BrowserExecutor's page actions and the safety policy via ActionRunner.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from playwright.async_api import Browser, Page, async_playwright
from test_browser import HeadlessSession

from jarvis.apps.youtube_flow import load_youtube_config
from jarvis.config import AgentSettings
from jarvis.executors.base import ActionRunner
from jarvis.executors.browser import BrowserExecutor
from jarvis.planner.llm import LLMReply, LLMUnavailable, Usage
from jarvis.safety.confirm import ScriptedConfirmer
from jarvis.safety.policy import SafetyPolicy
from jarvis.tiers.structured.agent import BrowserAgent
from jarvis.tiers.structured.observer import observe
from jarvis.tiers.structured.schema import AgentAction, AgentStep

SITES = Path(__file__).parents[1] / "evals" / "sites" / "agent"


def site(name: str) -> str:
    return SITES.joinpath(name).as_uri()


def el(prompt: str, name: str) -> int:
    """The [number] of the element whose name contains `name` in the snapshot the agent sent."""
    match = re.search(rf'\[(\d+)\] [^\n"]*"[^"\n]*{re.escape(name)}', prompt)
    assert match, f"no element named {name!r} in:\n{prompt}"
    return int(match.group(1))


Script = Callable[[str], AgentStep]


class ScriptPlanner:
    """Plays a fixed script; each entry sees the real prompt (with the real page snapshot)."""

    name, model = "openrouter", "scripted"

    def __init__(self, *script: Script | Exception) -> None:
        self.script = list(script)
        self.prompts: list[str] = []

    async def decide(
        self,
        system: str,
        user: str,
        schema: object = None,
        max_tokens: int = 512,
        image: bytes | None = None,
    ) -> LLMReply:
        self.prompts.append(user)
        nxt = self.script.pop(0) if self.script else LLMUnavailable("script over")
        if isinstance(nxt, Exception):
            raise nxt
        return LLMReply(nxt(user), self.name, self.model, 5.0, Usage(100, 0, 0, 20))


def step(status: str = "continue", message: str | None = None, *actions: AgentAction) -> AgentStep:
    return AgentStep(thought="â€¦", actions=list(actions), status=status, message=message)  # type: ignore[arg-type]


@pytest.fixture
async def browser() -> AsyncIterator[Browser]:
    async with async_playwright() as pw:
        try:
            b = await pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - test browser not installed
            pytest.skip(f"run `uv run playwright install chromium` ({exc})")
        yield b
        await b.close()


async def make(
    browser: Browser, start: str, planner: ScriptPlanner, answers: list[bool] | None = None,
    handoff_done: bool = True, settings: AgentSettings | None = None,
) -> tuple[BrowserAgent, Page, ScriptedConfirmer]:  # fmt: skip
    context = await browser.new_context()
    page = await context.new_page()
    await page.goto(site(start))
    session = HeadlessSession(context)
    confirmer = ScriptedConfirmer(answers, handoff_done)
    policy = SafetyPolicy.from_yaml()
    executor = BrowserExecutor(
        session, load_youtube_config(), policy.rules.captcha, confirmer, announce=_silent
    )
    runner = ActionRunner(policy, executor, confirmer, annotate=executor.annotate)
    executor.captcha.escalate = runner.run_action
    agent = BrowserAgent([planner], executor, runner.run_action, settings or AgentSettings())
    return agent, page, confirmer


async def _silent(_: str) -> None:
    return None


# ---- observer --------------------------------------------------------------------------------


async def test_observer_numbers_elements_with_their_labels(browser: Browser) -> None:
    page = await (await browser.new_context()).new_page()
    await page.goto(site("form.html"))
    snap = await observe(page)
    names = {e.name: e for e in snap.elements}
    assert {"Your name", "Country", "Send me news", "Send message"} <= set(names)
    assert names["Country"].tag == "select" and names["Country"].value == "India"
    assert names["Send me news"].checked is False
    assert await page.locator(f'[data-jarvis-id="{names["Your name"].id}"]').count() == 1
    assert "Contact us" in snap.headings and "Elements:" in snap.render()


async def test_observer_flags_password_fields_and_never_reads_them(browser: Browser) -> None:
    page = await (await browser.new_context()).new_page()
    await page.goto(site("login.html"))
    await page.fill("#p", "hunter2")
    snap = await observe(page)
    pw = next(e for e in snap.elements if e.password)
    assert pw.name == "Password" and pw.value == ""
    assert "hunter2" not in snap.render() and "(password field)" in snap.render()


async def test_observer_caps_elements(browser: Browser) -> None:
    page = await (await browser.new_context()).new_page()
    await page.goto(site("form.html"))
    snap = await observe(page, max_elements=2)
    assert len(snap.elements) == 2 and snap.more_below


# ---- the loop --------------------------------------------------------------------------------


async def test_finds_the_newest_video_on_a_channel(browser: Browser) -> None:
    planner = ScriptPlanner(
        lambda p: step("continue", None,
                       AgentAction(op="type", element=el(p, "Search videos"), text="Dream"),
                       AgentAction(op="press", element=el(p, "Search videos"), key="Enter")),
        lambda p: step("continue", None, AgentAction(op="click", element=el(p, "Dream\""))),
        lambda p: step("continue", None, AgentAction(op="click", element=el(p, "Videos"))),
        lambda p: step("continue", None, AgentAction(op="click", element=el(p, "I Survived"))),
        lambda p: step("done", "Playing I Survived 100 Days"),
    )  # fmt: skip
    agent, page, _ = await make(browser, "home.html", planner)
    outcome = await agent.run("play the latest video of Dream")
    assert outcome.ok and outcome.message == "Playing I Survived 100 Days"
    assert "v=new1" in page.url and outcome.actions == 5 and outcome.steps == 5
    assert "Results for: Dream" in planner.prompts[1]  # it really saw each new page


async def test_fills_a_form(browser: Browser) -> None:
    planner = ScriptPlanner(
        lambda p: step("continue", None,
                       AgentAction(op="type", element=el(p, "Your name"), text="Bhuvan"),
                       AgentAction(op="select", element=el(p, "Country"), text="Nepal"),
                       AgentAction(op="click", element=el(p, "Send me news"))),
        lambda p: step("continue", None,
                       AgentAction(op="click", element=el(p, "Send message"),
                                   risk_hint="irreversible")),
        lambda p: step("done", "Sent."),
    )  # fmt: skip
    # "Send me news" also asks (the word "send"): a needless question is the safe kind of mistake
    agent, page, confirmer = await make(browser, "form.html", planner, answers=[True, True])
    outcome = await agent.run("fill the contact form as Bhuvan from Nepal and send it")
    assert outcome.ok
    assert "Got: Bhuvan from Nepal" in await page.inner_text("body")
    assert any("Send message" in prompt for prompt in confirmer.prompts)  # asked before sending


async def test_delete_asks_first_and_no_means_no(browser: Browser) -> None:
    planner = ScriptPlanner(
        lambda p: step("continue", None, AgentAction(op="click", element=el(p, "Delete account"))),
    )
    agent, page, confirmer = await make(browser, "settings.html", planner, answers=[False])
    outcome = await agent.run("delete my account")
    assert not outcome.ok and outcome.error == "user_declined"
    assert "Account active" in await page.inner_text("body")  # nothing happened
    assert "Delete account" in confirmer.prompts[0]


async def test_planner_risk_hint_raises_risk(browser: Browser) -> None:
    """A harmless-looking label the planner knows is irreversible still gets confirmed."""
    planner = ScriptPlanner(
        lambda p: step(
            "continue", None,
            AgentAction(op="click", element=el(p, "Settings"), risk_hint="irreversible"),
        ),
    )  # fmt: skip
    agent, page, confirmer = await make(browser, "home.html", planner, answers=[False])
    outcome = await agent.run("x")
    assert outcome.error == "user_declined" and page.url.endswith("home.html")
    assert confirmer.prompts


async def test_password_fields_are_handed_to_the_user(browser: Browser) -> None:
    planner = ScriptPlanner(
        lambda p: step("continue", None,
                       AgentAction(op="type", element=el(p, "Password"), text="guess")),
        lambda p: step("done", "Signed in."),
    )  # fmt: skip
    agent, page, confirmer = await make(browser, "login.html", planner, handoff_done=True)
    outcome = await agent.run("sign in")
    assert outcome.ok
    assert await page.input_value("#p") == ""  # the agent never typed into it
    assert "I need you for this step" in confirmer.prompts[0]


async def test_element_that_isnt_there_is_reported_back(browser: Browser) -> None:
    planner = ScriptPlanner(
        lambda p: step("continue", None, AgentAction(op="click", element=999)),
        lambda p: step("fail", "Couldn't find it."),
    )
    agent, _, _ = await make(browser, "home.html", planner)
    outcome = await agent.run("x")
    assert outcome.error == "failed" and "isn't on the page" in planner.prompts[1]


async def test_stops_when_going_round_in_circles(browser: Browser) -> None:
    def scroll(p: str) -> AgentStep:
        return step("continue", None, AgentAction(op="scroll", text="down"))

    agent, _, _ = await make(browser, "home.html", ScriptPlanner(scroll, scroll, scroll))
    outcome = await agent.run("x")
    assert outcome.error == "stuck" and outcome.steps == 2


async def test_same_action_on_a_changing_page_is_progress(browser: Browser) -> None:
    def next_month(p: str) -> AgentStep:  # October → November → December → January
        return step("continue", None, AgentAction(op="click", element=el(p, "Next Month")))

    planner = ScriptPlanner(
        lambda p: step("continue", None, AgentAction(op="click", element=el(p, "Departure Date"))),
        next_month, next_month, next_month,
        lambda p: step("continue", None,
                       AgentAction(op="click", element=el(p, "Choose January 20,"))),
        lambda p: step("done", "Picked 20 January 2027."),
    )  # fmt: skip
    agent, page, _ = await make(browser, "travel.html", planner)
    outcome = await agent.run("pick 20 january 2027")
    assert outcome.ok, outcome.message
    assert await page.inner_text("#date span") == "20 Jan 2027"


async def test_stops_after_max_steps(browser: Browser) -> None:
    def scroll(p: str) -> AgentStep:
        return step("continue", None, AgentAction(op="scroll", text="down" if len(p) % 2 else "up"))

    planner = ScriptPlanner(*[scroll] * 10)
    agent, _, _ = await make(browser, "videos.html", planner, settings=AgentSettings(max_steps=2))
    outcome = await agent.run("x")
    assert outcome.error in ("max_steps", "stuck") and outcome.steps <= 2


async def test_one_planner_hiccup_is_retried(browser: Browser) -> None:
    planner = ScriptPlanner(LLMUnavailable("OpenRouter timed out"), lambda p: step("done", "Done."))
    agent, _, _ = await make(browser, "home.html", planner)
    outcome = await agent.run("x")
    assert outcome.ok and len(planner.prompts) == 2


async def test_no_planner_available(browser: Browser) -> None:
    planner = ScriptPlanner(LLMUnavailable("can't reach OpenRouter (offline?)"))
    agent, _, _ = await make(browser, "home.html", planner)
    outcome = await agent.run("x")
    assert outcome.error == "no_planner" and "can't reach OpenRouter" in outcome.message


async def test_ask_and_fail_end_the_run(browser: Browser) -> None:
    agent, _, _ = await make(
        browser, "home.html", ScriptPlanner(lambda p: step("ask", "Which one?"))
    )
    assert (await agent.run("x")).message == "Which one?"


async def test_page_text_reaches_the_planner_as_data(browser: Browser) -> None:
    planner = ScriptPlanner(lambda p: step("done", "Rs 3,499 on the 14th."))
    agent, page, _ = await make(browser, "injected.html", planner)
    outcome = await agent.run("what is the cheapest flight to goa")
    assert outcome.ok and "Rs 3,499" in planner.prompts[0]
    assert "Account active" in await page.inner_text("body")


async def test_observer_waits_for_late_content(browser: Browser) -> None:
    """Sites keep drawing after "load" (results arrive in stages): the snapshot waits until the
    page has been quiet for 0.5 s, so content that keeps arriving is included."""
    page = await (await browser.new_context()).new_page()
    await page.set_content(
        "<button>Early</button><script>"
        "const add = (t) => { const b = document.createElement('button');"
        " b.textContent = t; document.body.append(b); };"
        "setTimeout(() => add('Middle'), 300); setTimeout(() => add('Late'), 650);"
        "</script>"
    )
    names = {e.name for e in (await observe(page)).elements}
    assert {"Early", "Middle", "Late"} <= names


# ---- popups, custom controls, honest failures (from the real yatra.com test) -----------------


async def _travel_page(browser: Browser) -> Page:
    page = await (await browser.new_context(viewport={"width": 1280, "height": 800})).new_page()
    await page.goto(site("travel.html"))
    return page


async def test_observer_lists_custom_div_controls(browser: Browser) -> None:
    page = await _travel_page(browser)
    await page.click("#trav")  # opens the travellers popup (div-based numbers + "Done")
    snap = await observe(page)
    names = {e.name for e in snap.elements}
    assert {"1", "2", "Economy", "Business", "Done"} <= names  # inside the combobox: still listed
    assert "1 2 3 4" not in names  # the row is a group; its items are the controls
    assert "Departure From New Delhi inputbox" in names and "New Delhi" not in names  # one box


async def test_observer_hides_what_a_popup_covers_and_says_so(browser: Browser) -> None:
    page = await _travel_page(browser)
    assert "Search" in {e.name for e in (await observe(page)).elements}
    await page.click("#trav")
    snap = await observe(page)
    assert "Search" not in {e.name for e in snap.elements}  # behind the popup: not clickable
    assert snap.covered > 0 and 'large popup or overlay "Adult' in snap.render()
    assert snap.cover.startswith("Adult") and snap.cover_share > 0.25  # what covers, how big


async def test_click_on_covered_element_fails_fast_with_the_reason(browser: Browser) -> None:
    planner = ScriptPlanner()
    agent, page, _ = await make(browser, "travel.html", planner)
    await page.set_viewport_size({"width": 1280, "height": 800})
    snap = await observe(page)
    search = next(e.id for e in snap.elements if e.name == "Search")
    await page.click("#trav")  # the popup now covers Search
    from jarvis.models import Action

    result = await agent.run_action(
        Action(kind="click", target="Search", args={"element": search}, page_url=page.url)
    )
    assert result.error == "covered" and "Done" in (result.observation or "")
    assert result.duration_ms < 2500  # no waiting out a timeout


async def test_typing_into_a_custom_box_works(browser: Browser) -> None:
    planner = ScriptPlanner()
    agent, page, _ = await make(browser, "travel.html", planner)
    snap = await observe(page)
    box = next(e.id for e in snap.elements if e.name.startswith("Departure From"))
    from jarvis.models import Action

    result = await agent.run_action(
        Action(kind="type_text", target="Departure From", args={"element": box, "text": "Mum"},
               page_url=page.url)
    )  # fmt: skip
    assert result.ok, result.observation
    assert "Mumbai (BOM)" in await page.inner_text("#sug")


async def test_typing_that_pops_up_suggestions_ends_the_batch(browser: Browser) -> None:
    planner = ScriptPlanner(
        # a careless batch: type the city, then move on without picking a suggestion
        lambda p: step("continue", None,
                       AgentAction(op="type", element=el(p, "Departure From"), text="Lon"),
                       AgentAction(op="click", element=el(p, "Departure Date"))),
        lambda p: step("done", "Seen the suggestions."),
    )  # fmt: skip
    agent, page, _ = await make(browser, "travel.html", planner)
    outcome = await agent.run("x")
    assert outcome.ok and outcome.actions == 1  # the click was not done…
    assert '"London (LON) All airports"' in planner.prompts[1]  # …the planner saw the list first


async def test_a_redrawn_element_ends_the_batch_without_a_failure(browser: Browser) -> None:
    planner = ScriptPlanner(
        lambda p: step("continue", None, AgentAction(op="click", element=el(p, "Departure Date"))),
        # the calendar re-draws itself on every month change: the second number goes stale
        lambda p: step("continue", None, AgentAction(op="click", element=el(p, "Next Month")),
                       AgentAction(op="click", element=el(p, "Next Month"))),
        lambda p: step("done", "On December."),
    )  # fmt: skip
    agent, page, _ = await make(browser, "travel.html", planner)
    outcome = await agent.run("x")
    assert outcome.ok and outcome.actions == 2
    assert "failed" not in planner.prompts[2] and "November 2026" in planner.prompts[2]


async def test_a_box_shows_what_it_says_now_not_its_stale_label(browser: Browser) -> None:
    page = await _travel_page(browser)
    await page.click("#from")
    await page.fill("#from input", "Mum")
    await page.click("#sug div")
    names = {e.name: e for e in (await observe(page)).elements}
    assert names["Departure From New Delhi inputbox"].value == "Departure From Mumbai"


async def test_typing_into_a_plain_form_keeps_the_batch(browser: Browser) -> None:
    planner = ScriptPlanner(
        lambda p: step("continue", None,
                       AgentAction(op="type", element=el(p, "Your name"), text="Bhuvan"),
                       AgentAction(op="select", element=el(p, "Country"), text="Nepal")),
        lambda p: step("done", "Filled."),
    )  # fmt: skip
    agent, page, _ = await make(browser, "form.html", planner)
    outcome = await agent.run("x")
    assert outcome.ok and outcome.actions == 2 and outcome.steps == 2
    assert await page.input_value("#c") == "Nepal"


async def test_books_through_a_popup_that_opens_by_itself(browser: Browser) -> None:
    planner = ScriptPlanner(
        lambda p: step("continue", None,
                       AgentAction(op="type", element=el(p, "Departure From"), text="Mumbai")),
        lambda p: step("continue", None, AgentAction(op="click", element=el(p, "Mumbai (BOM)"))),
        lambda p: step("continue", None,
                       AgentAction(op="type", element=el(p, "Arrival At"), text="London")),
        lambda p: step("continue", None, AgentAction(op="click", element=el(p, "London (LON)"))),
        lambda p: step("continue", None, AgentAction(op="click", element=el(p, "Departure Date"))),
        lambda p: step("continue", None,
                       AgentAction(op="click", element=el(p, "Choose October 30,"))),
        # the travellers popup opened on its own and covers Search — the snapshot says so
        lambda p: step("continue", None, AgentAction(op="click", element=el(p, "Done"))),
        lambda p: step("continue", None, AgentAction(op="click", element=el(p, "Search"))),
        lambda p: step("done", "Here are the flights from Mumbai to London on 30 October."),
    )  # fmt: skip
    agent, page, _ = await make(browser, "travel.html", planner)
    await page.set_viewport_size({"width": 1280, "height": 800})
    outcome = await agent.run("search flights from Mumbai to London on 30 October 2026")
    assert outcome.ok, outcome.message
    assert "Flights from Mumbai to London on 2026-10-30" in await page.inner_text("h1")
    assert 'popup or overlay "Adult' in planner.prompts[6]


async def test_same_failure_twice_stops_early(browser: Browser) -> None:
    def broken_link(p: str) -> AgentStep:  # fails the same way every time (.invalid never resolves)
        return step(
            "continue", None, AgentAction(op="navigate", url="https://shop.example.invalid/")
        )

    agent, _, _ = await make(
        browser, "home.html", ScriptPlanner(broken_link, broken_link, broken_link)
    )
    outcome = await agent.run("open the shop")
    assert outcome.error == "stuck" and outcome.steps == 2 and "I'm stuck" in outcome.message


def test_final_message_drops_json_debris() -> None:
    from jarvis.tiers.structured.agent import tidy

    assert tidy("The cheapest is 46,921 rupees with one stop.}") == (
        "The cheapest is 46,921 rupees with one stop."
    )
    assert tidy('Done."}') == "Done."
    assert (
        tidy("Sorted from highest to lowest price.](#)") == "Sorted from highest to lowest price."
    )
    assert tidy('Unused and in its box.», ».replace("»","")') == "Unused and in its box."
    assert tidy("Opened the page.") == "Opened the page."
    assert tidy("It costs ₹1,44,900 (about $1,700).") == "It costs ₹1,44,900 (about $1,700)."
    assert tidy('Playing "Blinding Lights".') == 'Playing "Blinding Lights".'
    assert tidy("Scale is 125 percent.)") == "Scale is 125 percent."
    assert tidy("Sent with the name Bhuvan.','risk_hint':'none'") == "Sent with the name Bhuvan."
    assert tidy("Playing Blinding Lights in the Jarvis Test App.</br>") == (
        "Playing Blinding Lights in the Jarvis Test App."
    )


# ---- find, scrolling limits, harmless popups (from the real amazon.com test) ----------------


async def test_find_scrolls_to_words_anywhere_on_the_page(browser: Browser) -> None:
    planner = ScriptPlanner(
        lambda p: step("continue", None, AgentAction(op="find", text="returns policy")),
        lambda p: step("done", "Found it."),
    )
    agent, page, _ = await make(browser, "shop.html", planner)
    outcome = await agent.run("x")
    assert outcome.ok
    assert 'find "returns policy" → ok: Found "returns policy"' in planner.prompts[1]
    assert await page.locator("#returns").is_visible()  # scrolled there


async def test_find_says_when_words_are_not_on_the_page(browser: Browser) -> None:
    planner = ScriptPlanner(
        lambda p: step("continue", None, AgentAction(op="find", text="Price range")),
        lambda p: step("done", "There's no price filter."),
    )
    agent, _, _ = await make(browser, "shop.html", planner)
    await agent.run("x")
    assert '"Price range" is not anywhere on this page' in planner.prompts[1]


async def test_scrolling_in_a_row_gets_a_reminder_then_stops(browser: Browser) -> None:
    def scroll(p: str) -> AgentStep:
        return step("continue", None, AgentAction(op="scroll", text="down"))

    planner = ScriptPlanner(*[scroll] * 8)
    agent, _, _ = await make(browser, "feed.html", planner)  # endless: every scroll loads more
    outcome = await agent.run("find the price filter")
    hint = "scrolled 3 times in a row: if you are looking for something, use find"
    assert hint in planner.prompts[3]
    assert outcome.error == "stuck" and outcome.actions == 6 and "scrolled" in outcome.message


async def test_a_small_harmless_popup_is_named_and_can_be_ignored(browser: Browser) -> None:
    page = await (await browser.new_context(viewport={"width": 1280, "height": 800})).new_page()
    await page.goto(site("shop.html"))
    snap = await observe(page)
    assert snap.cover.startswith("We're showing you items that ship to India")
    assert snap.cover_share < 0.25
    note = snap.render()
    assert "small popup or overlay" in note and "otherwise ignore it" in note


async def test_a_key_that_changes_nothing_is_reported(browser: Browser) -> None:
    planner = ScriptPlanner(
        lambda p: step("continue", None, AgentAction(op="press", key="Escape")),
        lambda p: step("done", "ok"),
    )
    agent, _, _ = await make(browser, "shop.html", planner)
    await agent.run("x")
    assert "press Escape → ok, but nothing on the page changed" in planner.prompts[1]


async def test_sorting_finds_the_expensive_phones_without_a_price_filter(browser: Browser) -> None:
    planner = ScriptPlanner(
        lambda p: step("continue", None, AgentAction(op="find", text="Price")),
        lambda p: step("continue", None, AgentAction(op="select", element=el(p, "Sort by"),
                                                     text="Price: High to Low")),
        lambda p: step("done", "No price filter, so I sorted by price: iPhone 16 Pro Max …"),
    )  # fmt: skip
    agent, page, _ = await make(browser, "shop.html", planner)
    outcome = await agent.run("show phones of at least 80000 rupees")
    assert outcome.ok
    assert (await page.locator(".item h3").first.inner_text()) == "iPhone 16 Pro Max"
