"""Phase 15: the vision tier — a screenshot with numbered boxes when the text view isn't enough.

These tests use a fake box finder (boxes from the page's own layout) so they run without the GPU
model; the OmniParser detector itself is checked in a `desktop`-marked test.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from playwright.async_api import Browser, async_playwright
from test_browser import HeadlessSession

from jarvis.apps.youtube_flow import load_youtube_config
from jarvis.config import AgentSettings
from jarvis.executors.base import ActionRunner
from jarvis.executors.browser import BrowserExecutor
from jarvis.models import RiskLevel
from jarvis.planner.llm import LLMReply, Usage
from jarvis.safety.confirm import ScriptedConfirmer
from jarvis.safety.policy import Decision, SafetyPolicy
from jarvis.tiers.structured.agent import BrowserAgent, box_action
from jarvis.tiers.structured.evaluate import SITES
from jarvis.tiers.structured.observer import Snapshot
from jarvis.tiers.structured.schema import AgentAction, AgentStep
from jarvis.tiers.structured.surface import Shot
from jarvis.tiers.vision.detector import Box, tidy_boxes
from jarvis.tiers.vision.eyes import Eyes, Look
from jarvis.tiers.vision.marks import draw_marks


def step(status: str = "continue", message: str | None = None, *actions: AgentAction) -> AgentStep:
    return AgentStep(thought="…", actions=list(actions), status=status, message=message)  # type: ignore[arg-type]


class SeeingPlanner:
    """Scripted planner that records whether it was shown a picture."""

    name, model = "openrouter", "scripted"

    def __init__(self, *script: Callable[[str], AgentStep]) -> None:
        self.script = list(script)
        self.prompts: list[str] = []
        self.images: list[bytes | None] = []

    async def decide(self, system: str, user: str, schema: Any = None, max_tokens: int = 0,
                     image: bytes | None = None) -> LLMReply:  # fmt: skip
        self.prompts.append(user)
        self.images.append(image)
        return LLMReply(self.script.pop(0)(user), self.name, self.model, 1.0, Usage())


class LayoutFinder:
    """Stands in for OmniParser: the boxes of the given page elements (CSS pixels)."""

    def __init__(self) -> None:
        self.boxes: list[Box] = []

    def detect(self, image: object) -> list[Box]:
        return self.boxes


@pytest.fixture
async def browser() -> AsyncIterator[Browser]:
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        yield b
        await b.close()


# ---- pieces ----------------------------------------------------------------------------------


def test_boxes_lose_duplicates_and_read_like_a_page() -> None:
    boxes = tidy_boxes([
        Box(300, 10, 340, 40, 0.9), Box(10, 10, 50, 40, 0.8), Box(12, 12, 48, 38, 0.3),  # dup
        Box(10, 200, 50, 230, 0.7),
    ])  # fmt: skip
    assert [(b.x1, b.y1) for b in boxes] == [(10, 10), (300, 10), (10, 200)]


def test_marks_are_drawn_as_a_jpeg() -> None:
    from PIL import Image

    jpeg = draw_marks(Image.new("RGB", (400, 300), "white"), [Box(10, 10, 100, 60)])
    assert jpeg[:2] == b"\xff\xd8"  # a JPEG, in memory only


def test_a_box_click_carries_its_label_for_the_safety_check() -> None:
    from PIL import Image

    look = Look(Shot(Image.new("RGB", (10, 10))), [Box(100, 200, 140, 240)], b"")
    snap = Snapshot(url="https://shop.example/checkout/review", title="Review")
    action, _ = box_action(AgentAction(op="click", element=1, label="Place your order"), look,
                           snap, "browser")  # fmt: skip
    assert action is not None and action.kind == "click_at" and action.args["x"] == 120
    policy = SafetyPolicy.from_yaml()
    assert policy.classify(action)[0] == RiskLevel.PAYMENT  # read back + yes, like any order
    unlabelled, _ = box_action(AgentAction(op="click", element=1), look, snap, "browser")
    assert unlabelled is not None
    plain = unlabelled.model_copy(update={"page_url": "https://example.com"})
    assert policy.check(plain).decision == Decision.CONFIRM  # can't say what it clicks → asks
    missing, why = box_action(AgentAction(op="click", element=9, label="x"), look, snap, "browser")
    assert missing is None and "no box 9" in (why or "")


# ---- the loop: text first, a look when the text view isn't enough ----------------------------


async def make(
    browser: Browser, page_name: str, planner: SeeingPlanner, finder: LayoutFinder,
    answers: list[bool] | None = None,
) -> tuple[BrowserAgent, Any, ScriptedConfirmer]:  # fmt: skip
    context = await browser.new_context(viewport={"width": 1000, "height": 700})
    page = await context.new_page()
    await page.goto(SITES.joinpath(page_name).as_uri())
    confirmer = ScriptedConfirmer(answers or [])
    policy = SafetyPolicy.from_yaml()
    executor = BrowserExecutor(HeadlessSession(context), load_youtube_config(),
                               policy.rules.captcha, confirmer, announce=_silent)  # fmt: skip
    runner = ActionRunner(policy, executor, confirmer, annotate=executor.annotate)
    agent = BrowserAgent([planner], executor, runner.run_action, AgentSettings(),
                         eyes=Eyes(finder))  # fmt: skip
    return agent, page, confirmer


async def _silent(_: str) -> None:
    return None


async def test_look_then_click_a_colour_swatch_that_has_no_name(browser: Browser) -> None:
    finder = LayoutFinder()
    planner = SeeingPlanner(
        lambda p: step("continue", None, AgentAction(op="look")),  # the text can't tell colours
        lambda p: step("continue", None, AgentAction(op="click", element=2, label="blue swatch")),
        lambda p: step("done", "Blue is selected."),
    )
    agent, page, _ = await make(browser, "vision-swatch.html", planner, finder)
    rects = await page.eval_on_selector_all(
        ".sw", "els => els.map(e => { const r = e.getBoundingClientRect(); "
        "return [r.left, r.top, r.right, r.bottom]; })")  # fmt: skip
    finder.boxes = [Box(*r) for r in rects]
    outcome = await agent.run("choose the blue colour")
    assert outcome.ok
    assert planner.images[0] is None and planner.images[1] is not None  # text first, then a look
    assert "numbered boxes" in planner.prompts[1]
    assert "Selected colour: blue" in await page.inner_text("#sel")
    assert planner.images[2] is None  # back to text after the look


async def test_a_styled_buy_box_found_by_looking_still_gets_the_order_read_back(
    browser: Browser,
) -> None:
    finder = LayoutFinder()
    planner = SeeingPlanner(
        lambda p: step("continue", None, AgentAction(op="look")),
        lambda p: step("continue", None,
                       AgentAction(op="click", element=2, label="Buy now", risk_hint="payment")),
        lambda p: step("done", "…"),
    )  # fmt: skip
    agent, page, confirmer = await make(browser, "vision-buy.html", planner, finder)
    rects = await page.eval_on_selector_all(
        ".pill", "els => els.map(e => { const r = e.getBoundingClientRect(); "
        "return [r.left, r.top, r.right, r.bottom]; })")  # fmt: skip
    finder.boxes = [Box(*r) for r in rects]
    outcome = await agent.run("buy this phone now")
    # "Buy now" may place an order: on a page without address / payment it's refused, never silent
    assert "vision-buy.html" in page.url and confirmer.prompts == []
    assert outcome.actions >= 1


def test_click_points_map_back_through_scale_and_origin() -> None:
    from PIL import Image

    shot = Shot(Image.new("RGB", (10, 10)), origin=(500, 300), scale=1.0)
    look = Look(shot, [Box(10, 20, 30, 40)], b"")
    assert look.click_point(1) == (520, 330) and look.click_point(2) is None


@pytest.mark.desktop
def test_omniparser_finds_the_buttons_on_a_screenshot() -> None:
    """The real detector (downloads ~40 MB once; GPU if available)."""
    from PIL import Image, ImageDraw

    from jarvis.tiers.vision.detector import Detector

    picture = Image.new("RGB", (800, 400), "white")
    draw = ImageDraw.Draw(picture)
    for x in (60, 320, 580):
        draw.rounded_rectangle((x, 160, x + 160, 220), radius=10, fill="#2563eb")
        draw.text((x + 50, 182), "Button", fill="white")
    boxes = Detector().detect(picture)
    centres = [b.centre for b in boxes]
    for x in (140, 400, 660):
        assert any(abs(cx - x) < 60 and abs(cy - 190) < 40 for cx, cy in centres), centres
