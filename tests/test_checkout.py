"""Phase 11.4: the checkout guard — order read off the page, word for word, then a yes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from playwright.async_api import Browser, async_playwright

from jarvis.executors.base import ActionRunner
from jarvis.models import Action, ActionResult, RiskLevel
from jarvis.planner.llm import LLMReply, LLMUnavailable, Usage
from jarvis.safety.checkout import VISIBLE_TEXT_JS, OrderReader, OrderSummary, normalize, on_page
from jarvis.safety.confirm import ScriptedConfirmer
from jarvis.safety.policy import Decision, SafetyPolicy
from jarvis.tiers.structured.evaluate import SITES

PAGE = (
    "Review your order Delivery address [selected] Bhuvan Mopidi, 12 Lake Road, Hyderabad 500032 "
    "Payment method [selected] UPI (saved: bhuvan@okbank) Order summary boAt Airdopes 141 (Bold "
    "Black) — Qty: 1 Order total: ₹1,299 Place your order"
)
GOOD = OrderSummary(
    item="boAt Airdopes 141 (Bold Black)", quantity="1", price="₹1,299",
    address="Bhuvan Mopidi, 12 Lake Road, Hyderabad 500032",
    payment_method="UPI (saved: bhuvan@okbank)",
)  # fmt: skip


class FakeReaderModel:
    name, model = "openrouter", "fake"

    def __init__(self, *replies: OrderSummary | Exception) -> None:
        self.replies = list(replies)

    async def decide(
        self,
        system: str,
        user: str,
        schema: Any = None,
        max_tokens: int = 0,
        image: bytes | None = None,
    ) -> Any:
        reply = self.replies.pop(0) if self.replies else LLMUnavailable("none left")
        if isinstance(reply, Exception):
            raise reply
        return LLMReply(reply, self.name, self.model, 1.0, Usage())


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[Action] = []

    async def execute(self, action: Action) -> ActionResult:
        self.calls.append(action)
        return ActionResult(ok=True, observation="clicked")


def reader(*replies: OrderSummary | Exception, page: str = PAGE) -> OrderReader:
    async def text() -> str:
        return page

    return OrderReader([FakeReaderModel(*replies)], text)


PLACE = Action(kind="click", target="Place your order",
               page_url="https://shop.example/checkout/review")  # fmt: skip


# ---- word for word ---------------------------------------------------------------------------


def test_values_must_be_on_the_page_word_for_word() -> None:
    assert on_page("₹1,299", PAGE) and on_page("Rs. 1299", PAGE) and on_page("rs 1 299", PAGE)
    assert on_page("bhuvan mopidi,  12 lake road, hyderabad 500032", PAGE)
    assert not on_page("₹9", PAGE) and not on_page("₹1,199", PAGE)
    assert not on_page("Warangal", PAGE) and not on_page("", PAGE)
    assert normalize("₹ 1,29,900") == "rs 129900"


async def test_reader_fills_in_what_is_shown() -> None:
    action = await reader(GOOD)(PLACE)
    assert action.args["price"] == "₹1,299" and action.args["payment_method"].startswith("UPI")


async def test_a_value_not_on_the_page_is_dropped() -> None:
    planted = GOOD.model_copy(update={"price": "₹9"})  # e.g. read from injected / hidden text
    action = await reader(planted)(PLACE)
    assert "price" not in action.args and "₹9" in action.args["order_unverified"]


async def test_a_hiccup_gets_a_second_try() -> None:
    action = await reader(LLMUnavailable("timeout"), GOOD)(PLACE)
    assert action.args["item"].startswith("boAt")


# ---- the policy and the runner ---------------------------------------------------------------


def test_only_the_final_step_on_a_checkout_page_is_gated() -> None:
    policy = SafetyPolicy.from_yaml()
    url = "https://www.amazon.in/gp/buy/spc/handlers/display.html"
    choose = Action(kind="click", target="Bhuvan Mopidi, 12 Lake Road", page_url=url)
    assert policy.check(choose).decision == Decision.ALLOW  # user's choice: free
    for label in ("Place your order", "Place order", "Pay now", "Buy now", "Confirm order"):
        risk = policy.classify(Action(kind="click", target=label, page_url=url))[0]
        assert risk == RiskLevel.PAYMENT, label
    hinted = Action(kind="click", target="Continue", page_url=url, flags={"agent_payment"})
    assert policy.classify(hinted)[0] == RiskLevel.PAYMENT  # the planner said it pays


@pytest.mark.parametrize(
    "field", ["Card number", "CVV", "Expiry (MM/YY)", "UPI ID", "Name on card"]
)
def test_card_and_upi_fields_are_the_users(field: str) -> None:
    action = Action(kind="type_text", target=field, args={"text": "4111 1111"})
    assert SafetyPolicy.from_yaml().check(action).decision == Decision.HANDOFF


async def test_place_order_reads_back_everything_then_asks() -> None:
    executor, confirmer = RecordingExecutor(), ScriptedConfirmer([True])
    runner = ActionRunner(SafetyPolicy.from_yaml(), executor, confirmer, order_reader=reader(GOOD))
    result = await runner.run_action(PLACE)
    assert result.ok and len(executor.calls) == 1
    question = confirmer.prompts[0]
    for part in ("Airdopes 141", "quantity 1", "₹1,299", "Hyderabad 500032", "UPI"):
        assert part in question
    assert result.data["confirmed"]  # never repeated in the same task


async def test_place_order_declined_never_clicks() -> None:
    executor, confirmer = RecordingExecutor(), ScriptedConfirmer([False])
    runner = ActionRunner(SafetyPolicy.from_yaml(), executor, confirmer, order_reader=reader(GOOD))
    assert (await runner.run_action(PLACE)).error == "user_declined" and executor.calls == []


async def test_missing_details_are_refused_without_asking() -> None:
    no_address = GOOD.model_copy(update={"address": None})
    executor, confirmer = RecordingExecutor(), ScriptedConfirmer([True])
    runner = ActionRunner(SafetyPolicy.from_yaml(), executor, confirmer,
                          order_reader=reader(no_address))  # fmt: skip
    result = await runner.run_action(PLACE)
    assert result.error == "order_details_missing" and "address" in result.observation
    assert executor.calls == [] and confirmer.prompts == []


# ---- what the page really shows --------------------------------------------------------------


@pytest.fixture
async def browser() -> AsyncIterator[Browser]:
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        yield b
        await b.close()


async def test_visible_text_leaves_out_planted_text_and_marks_choices(browser: Browser) -> None:
    page = await browser.new_page()
    await page.goto((SITES / "checkout" / "review.html").as_uri() + "?qty=2")
    text = await page.evaluate(VISIBLE_TEXT_JS, 20000)
    assert "₹9" not in text  # off the page: a person can't see it
    assert "[selected] Bhuvan Mopidi, 12 Lake Road" in text and "[selected] UPI" in text
    assert "Qty: 2" in text and "₹2,598" in text


async def test_a_field_left_out_gets_a_second_read() -> None:
    partial = GOOD.model_copy(update={"address": None, "payment_method": None})
    action = await reader(partial, GOOD)(PLACE)
    assert action.args["address"].endswith("500032") and action.args["payment_method"].startswith(
        "UPI"
    )
