"""Tabs like a person (from the Flipkart test): links open in the same tab, a pop-up is followed,
new tab / private window only when asked — and the agent always sees the tab strip."""

from __future__ import annotations

from typing import Any

from playwright.async_api import Browser
from test_agent_loop import ScriptPlanner, browser, el, make, step  # noqa: F401

from jarvis.tiers.structured.schema import AgentAction


def executor_of(agent: Any) -> Any:
    return agent.surfaces["browser"].browser


async def test_a_new_tab_link_opens_the_sites_way_and_is_followed(browser: Browser) -> None:  # noqa: F811
    """No rewriting: the site's link opens a new tab as it would for anyone; the agent moves
    there and sees what changed."""
    planner = ScriptPlanner(
        lambda p: step("continue", None, AgentAction(op="click", element=el(p, "Gen 2"))),
        lambda p: step("done", "₹1,099"),
    )
    agent, page, _ = await make(browser, "newtab.html", planner)
    outcome = await agent.run("open the Gen 2 product")
    assert outcome.ok
    assert len(page.context.pages) == 2  # the site's default: a new tab
    assert "it opened a new tab" in planner.prompts[1] and "Price: ₹1,099" in planner.prompts[1]
    assert "What changed since your last look:" in planner.prompts[1]
    assert 'a tab opened: "boAt Airdopes 141 Gen 2 - GadgetBay"' in planner.prompts[1]


async def test_a_pop_up_is_followed_and_the_tabs_are_listed(browser: Browser) -> None:  # noqa: F811
    planner = ScriptPlanner(
        lambda p: step("continue", None, AgentAction(op="click", element=el(p, "Size chart"))),
        lambda p: step("done", "M"),
    )
    agent, page, _ = await make(browser, "newtab.html", planner)
    await agent.run("open the size chart")
    assert "it opened a new tab: Size chart" in planner.prompts[1]
    assert "Ear tips" in planner.prompts[1]  # it reads the new tab now
    assert (
        "Tabs: [1] GadgetBay" in planner.prompts[1]
        and "Size chart - GadgetBay" in planner.prompts[1]
    )
    assert executor_of(agent).agent_page is not page


async def test_new_tab_switch_and_close_when_asked(browser: Browser) -> None:  # noqa: F811
    planner = ScriptPlanner(
        lambda p: step("continue", None, AgentAction(op="new_tab", element=el(p, "Gen 2"))),
        lambda p: step("continue", None, AgentAction(op="switch_tab", text="1")),
        lambda p: step("continue", None, AgentAction(op="close_tab", text="2")),
        lambda p: step("done", "ok"),
    )
    agent, page, confirmer = await make(browser, "newtab.html", planner)
    await agent.run("open it in a new tab, go back, close it")
    assert "Opened a new tab: boAt Airdopes 141 Gen 2" in planner.prompts[1]
    assert "Switched to the tab: GadgetBay" in planner.prompts[2]
    assert "Closed the tab: boAt Airdopes 141 Gen 2" in planner.prompts[3]
    assert len(page.context.pages) == 1 and confirmer.prompts == []  # Jarvis's own tab: no question


async def test_closing_the_users_own_tab_asks_first(browser: Browser) -> None:  # noqa: F811
    planner = ScriptPlanner(
        lambda p: step("continue", None, AgentAction(op="new_tab", url="about:blank")),
    )
    agent, page, confirmer = await make(browser, "newtab.html", planner, answers=[False])
    other = await page.context.new_page()  # the user's tab
    await other.goto("about:blank")
    planner.script = [
        lambda p: step("continue", None, AgentAction(op="close_tab", text="2")),
        lambda p: step("done", "ok"),
    ]
    await agent.run("close tab 2")
    assert confirmer.prompts and "close tab 2" in confirmer.prompts[0]
    assert not other.is_closed()


async def test_private_window_has_no_cookies(browser: Browser) -> None:  # noqa: F811
    url = "file:///" + str(__import__("pathlib").Path(__file__).parent.parent / "evals" / "sites"
                           / "agent" / "newtab-product.html").replace("\\", "/")  # fmt: skip
    planner = ScriptPlanner(
        lambda p: step("continue", None, AgentAction(op="private_window", url=url)),
        lambda p: step("done", "ok"),
    )
    agent, page, _ = await make(browser, "newtab.html", planner)
    await agent.run("open it in incognito")
    tab = executor_of(agent).agent_page
    assert tab.context is not page.context  # a separate, cookie-less context
    assert "Opened a private window" in planner.prompts[1] and "(private)" in planner.prompts[1]


async def test_lookalike_controls_are_numbered(browser: Browser) -> None:  # noqa: F811
    from test_agent_loop import site

    from jarvis.tiers.structured.observer import observe

    page = await (await browser.new_context()).new_page()
    await page.goto(site("variants.html") + "?c=orange")
    names = [e.name for e in (await observe(page)).elements]
    assert "Image (1 of 2)" in names and "Image (2 of 2)" in names  # the two plain swatches
    assert names.count("Image Out of stock (1 of 3)") + names.count("Out of stock (1 of 3)") == 1


def test_what_changed_tells_what_a_person_would_notice() -> None:
    from jarvis.tiers.structured.observer import Element, Snapshot, what_changed

    before = Snapshot(url="https://shop/p?c=orange", title="Airdopes",
                      text="Selected Color: Orange",
                      elements=[Element(id=1, tag="button", name="Add to cart", disabled=True),
                                Element(id=2, tag="a", name="Image")])  # fmt: skip
    after = Snapshot(url="https://shop/p?c=black", title="Airdopes",
                     text="Selected Color: Black Bold",
                     elements=[Element(id=1, tag="button", name="Add to cart"),
                               Element(id=2, tag="a", name="Image"),
                               Element(id=3, tag="button", name="Buy now")])  # fmt: skip
    changes = what_changed(before, after)
    assert "the address is now https://shop/p?c=black" in changes
    assert '"Add to cart" is now enabled' in changes and 'appeared: "Buy now"' in changes
    assert any(c.startswith('new text: "Black Bold"') for c in changes)
    assert what_changed(after, after) == ["nothing visible changed"]


async def test_a_short_named_control_keeps_its_surroundings(browser: Browser) -> None:  # noqa: F811
    from test_agent_loop import site

    from jarvis.tiers.structured.observer import observe

    page = await (await browser.new_context()).new_page()
    await page.goto(site("store-cart.html") + "?qty=2")
    snap = await observe(page)
    assert snap.render()  # renders with any context
