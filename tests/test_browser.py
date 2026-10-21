"""Browser flows against local fake pages in Playwright's headless Chromium (no network, no Chrome).

Skipped if the test browser isn't installed: `uv run playwright install chromium`.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from jarvis.apps.youtube_flow import ErrorPage, YouTubeConfig, load_youtube_config
from jarvis.executors.base import ActionRunner
from jarvis.executors.browser import BrowserExecutor
from jarvis.executors.browser_session import BrowserUnavailable
from jarvis.models import Action
from jarvis.safety.confirm import ScriptedConfirmer
from jarvis.safety.policy import SafetyPolicy

pytest.importorskip("playwright")
from playwright.async_api import Browser, BrowserContext, Page, async_playwright  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "browser"


def url(name: str, query: str = "") -> str:
    return FIXTURES.joinpath(name).as_uri() + (f"?{query}" if query else "")


class HeadlessSession:
    """PageSource backed by a headless Chromium context (the real one drives the Jarvis Chrome)."""

    def __init__(self, context: BrowserContext) -> None:
        self.context = context

    async def pages(self) -> list[Page]:
        return list(self.context.pages) or [await self.context.new_page()]

    async def new_page(self) -> Page:
        return await self.context.new_page()

    def open_pages(self) -> list[Page]:
        return list(self.context.pages)

    async def reset(self) -> None:
        self.resets = getattr(self, "resets", 0) + 1


class DownSession:
    """Chrome can't be reached."""

    async def pages(self) -> list[Page]:
        raise BrowserUnavailable("Chrome didn't open its control port")

    async def new_page(self) -> Page:
        raise BrowserUnavailable("Chrome didn't open its control port")

    def open_pages(self) -> list[Page]:
        return []

    async def reset(self) -> None:
        return None


class WaitingConfirmer(ScriptedConfirmer):
    """A user who never answers a handoff (so only the CAPTCHA disappearing can resume)."""

    async def handoff(self, message: str) -> bool:
        self.prompts.append(message)
        await asyncio.Event().wait()
        return True


@pytest.fixture
async def browser() -> AsyncIterator[Browser]:
    async with async_playwright() as pw:
        try:
            b = await pw.chromium.launch(headless=True)
        except Exception as exc:  # browser not downloaded
            pytest.skip(
                f"test browser missing ({exc.__class__.__name__}); run: playwright install chromium"
            )
        yield b
        await b.close()


@pytest.fixture
async def session(browser: Browser) -> AsyncIterator[HeadlessSession]:
    context = await browser.new_context()
    await context.route(re.compile(r"^https?://"), lambda route: route.abort())  # offline
    yield HeadlessSession(context)
    await context.close()


def yt_config(watch: str = "watch.html", ads: str = "ask") -> YouTubeConfig:
    cfg = load_youtube_config()  # the real selectors from config/apps/youtube.yaml
    search = url("results.html", f"watch={watch}&search_query={{query}}")
    return cfg.model_copy(
        update={"search_url": search, "ads": ads, "ask_timeout_s": 1.0,
                "timeouts_s": cfg.timeouts_s.model_copy(update={"start": 3, "ads_max": 10})}
    )  # fmt: skip


def make(
    session: object, cfg: YouTubeConfig | None = None, confirmer: ScriptedConfirmer | None = None
) -> tuple[ActionRunner, BrowserExecutor, ScriptedConfirmer, list[str]]:
    policy = SafetyPolicy.from_yaml()
    confirmer = confirmer or ScriptedConfirmer()
    opened: list[str] = []
    executor = BrowserExecutor(
        session,  # type: ignore[arg-type]
        cfg or yt_config(),
        policy.rules.captcha,
        confirmer,
        announce=lambda _: asyncio.sleep(0),
        opener=opened.append,
    )
    runner = ActionRunner(policy, executor, confirmer, annotate=executor.annotate)
    executor.captcha.escalate = runner.run_action
    return runner, executor, confirmer, opened


async def test_youtube_play_picks_first_real_video(session: HeadlessSession) -> None:
    runner, *_ = make(session)
    result = await runner.run_action(Action(kind="youtube_play", target="lofi"))
    assert result.ok, result.observation
    assert result.observation == "Playing: Lofi Girl - beats to study to"  # not ad/short/live/mix
    page = (await session.pages())[-1]
    assert "v=real" in page.url


async def test_ad_asked_and_skipped_on_yes(session: HeadlessSession) -> None:
    confirmer = ScriptedConfirmer([True])
    runner, *_ = make(session, yt_config("watch_ad.html"), confirmer)
    result = await runner.run_action(Action(kind="youtube_play", target="lofi"))
    assert result.ok, result.observation
    assert confirmer.prompts == ["There's an ad. Skip it?"]
    page = (await session.pages())[-1]
    assert await page.evaluate("window.adSkipped") is True
    assert await page.evaluate("document.querySelector('video').muted") is False  # restored


async def test_ad_left_alone_on_no(session: HeadlessSession) -> None:
    confirmer = ScriptedConfirmer([False])
    runner, *_ = make(session, yt_config("watch_ad.html"), confirmer)
    result = await runner.run_action(Action(kind="youtube_play", target="lofi"))
    assert result.ok, result.observation  # the ad ends by itself, then the video plays
    page = (await session.pages())[-1]
    assert await page.evaluate("window.adSkipped") is False
    assert "s of ads)" in result.observation  # time spent in ads is reported


async def test_skip_click_is_retried_when_it_misses(session: HeadlessSession) -> None:
    runner, *_ = make(session, yt_config("watch_ad.html?sticky", ads="skip"))
    assert (await runner.run_action(Action(kind="youtube_play", target="lofi"))).ok
    page = (await session.pages())[-1]
    assert await page.evaluate("window.adSkipped") is True
    assert await page.evaluate("window.skipClicks") == 2


async def test_ad_policy_skip_never_asks(session: HeadlessSession) -> None:
    confirmer = ScriptedConfirmer()
    runner, *_ = make(session, yt_config("watch_ad.html"), confirmer)
    assert (await runner.run_action(Action(kind="set_ad_policy", target="always"))).ok
    assert (await runner.run_action(Action(kind="youtube_play", target="lofi"))).ok
    assert confirmer.prompts == []
    assert await (await session.pages())[-1].evaluate("window.adSkipped") is True


async def test_pause_and_resume_current_video(session: HeadlessSession) -> None:
    runner, *_ = make(session)
    assert (await runner.run_action(Action(kind="youtube_play", target="lofi"))).ok
    page = (await session.pages())[-1]
    assert (await runner.run_action(Action(kind="media_key", target="pause"))).ok
    assert await page.evaluate("document.querySelector('video').paused") is True
    assert (await runner.run_action(Action(kind="media_key", target="resume"))).ok
    assert await page.evaluate("document.querySelector('video').paused") is False


async def test_next_video_presses_youtube_shortcut(session: HeadlessSession) -> None:
    runner, *_ = make(session)
    assert (await runner.run_action(Action(kind="youtube_play", target="lofi"))).ok
    assert (await runner.run_action(Action(kind="media_key", target="next"))).ok
    assert "Shift+N" in await (await session.pages())[-1].evaluate("window.keys")


async def test_captcha_handed_off_and_resumed_when_cleared(session: HeadlessSession) -> None:
    confirmer = WaitingConfirmer()
    runner, *_ = make(session, confirmer=confirmer)
    target = url("captcha.html", "solve=600")
    result = await runner.run_action(Action(kind="open_url", target=target))
    assert result.ok, result.observation  # resumed on its own once the check disappeared
    assert len(confirmer.prompts) == 1 and "human check" in confirmer.prompts[0]


async def test_captcha_cancelled_by_user(session: HeadlessSession) -> None:
    confirmer = ScriptedConfirmer(handoff_done=False)
    runner, *_ = make(session, confirmer=confirmer)
    result = await runner.run_action(Action(kind="open_url", target=url("captcha.html")))
    assert result.error == "user_cancelled"


async def test_checkout_page_raises_risk_of_media_keys(session: HeadlessSession) -> None:
    runner, *_ = make(session)
    page = await session.new_page()
    await page.goto(url("checkout/pay.html"))
    result = await runner.run_action(Action(kind="media_key", target="pause"))
    assert (
        result.error == "order_details_missing"
    )  # PAYMENT risk from the page, with no payment details


async def test_never_uses_default_browser_when_chrome_is_down() -> None:
    runner, _, _, opened = make(DownSession())
    for action in [Action(kind="youtube_play", target="lofi"),
                   Action(kind="open_url", target="https://www.instagram.com"),
                   Action(kind="browser_nav", target="back")]:  # fmt: skip
        result = await runner.run_action(action)
        assert result.error == "browser_unavailable", action.kind
    assert opened == []  # websites only ever open in the Jarvis Chrome window


async def test_default_browser_only_when_automation_is_off() -> None:
    runner, _, _, opened = make(None)  # browser.enabled: false
    result = await runner.run_action(Action(kind="open_url", target="https://x.com"))
    assert result.ok and opened == ["https://x.com"]


async def test_next_video_fails_without_a_video(session: HeadlessSession) -> None:
    runner, *_ = make(session)
    page = await session.new_page()
    await page.goto(url("results.html"))
    result = await runner.run_action(Action(kind="media_key", target="next"))
    assert result.error == "nothing_playing"  # never report success for a key that did nothing


async def test_youtube_error_page_fails_fast_with_reason(session: HeadlessSession) -> None:
    cfg = yt_config().model_copy(
        update={
            "search_url": url("redirect_oops.html", "search_query={query}"),
            "error_pages": [
                ErrorPage(match="oops.html", message="YouTube showed an account error.")
            ],
        }
    )
    runner, *_ = make(session, cfg)
    started = asyncio.get_running_loop().time()
    result = await runner.run_action(Action(kind="youtube_play", target="mr beast"))
    assert (result.error, result.observation) == (
        "youtube_error",
        "YouTube showed an account error.",
    )
    assert asyncio.get_running_loop().time() - started < 3  # not the 10 s results timeout


async def test_seek_restart_and_speed(session: HeadlessSession) -> None:
    runner, *_ = make(session)
    assert (await runner.run_action(Action(kind="youtube_play", target="lofi"))).ok
    page = (await session.pages())[-1]

    async def media(target: str, **args: str) -> str:
        result = await runner.run_action(Action(kind="media_key", target=target, args=args))
        assert result.ok, result.observation
        return result.observation

    t = "document.querySelector('video').currentTime"
    assert (
        await media("seek", direction="forward", amount="10", unit="min") == "At 10:01 of 1:00:00"
    )
    assert await page.evaluate(t) == 601
    await media("seek", direction="back", amount="30", unit="seconds")
    assert await page.evaluate(t) == 571
    await media("seek_to", time="5:30")
    assert await page.evaluate(t) == 330
    await media("seek_to", amount="2", unit="hours")  # past the end → clamped just before it
    assert await page.evaluate(t) == 3599
    await media("seek", direction="back", amount="3", unit="h")  # before the start → 0
    assert await page.evaluate(t) == 0
    await media("seek_to", time="0:00")  # zero is a valid time
    await media("restart")
    assert await media("speed", rate="1.5") == "Speed 1.5x"
    assert await media("speed", step="0.25") == "Speed 1.75x"
    assert await media("speed", rate="9") == "Speed 2x"  # clamped


async def test_seek_refused_on_live_stream(session: HeadlessSession) -> None:
    runner, *_ = make(session)
    assert (await runner.run_action(Action(kind="youtube_play", target="lofi"))).ok
    await (await session.pages())[-1].evaluate("window.FAKE_DURATION = Infinity")
    args = {"direction": "forward", "amount": "10", "unit": "min"}
    result = await runner.run_action(Action(kind="media_key", target="seek", args=args))
    assert result.error == "live_stream"


class ClosingSession(HeadlessSession):
    """The window is gone: every tab Jarvis gets is closed under it (`closes` times), like after
    the user closed the Jarvis Chrome window."""

    def __init__(self, context: BrowserContext, closes: int = 2) -> None:
        super().__init__(context)
        self.closes = closes
        self.resets = 0

    async def pages(self) -> list[Page]:
        pages = await super().pages()
        if self.closes > 0:
            self.closes -= 1
            await pages[-1].close()  # closed under Jarvis' feet
        return pages

    async def new_page(self) -> Page:
        page = await super().new_page()
        if self.closes > 0:
            self.closes -= 1
            await page.close()
        return page

    async def reset(self) -> None:
        self.resets += 1


async def test_window_closed_mid_action_reopens_and_retries(browser: Browser) -> None:
    session = ClosingSession(await browser.new_context())
    runner, *_ = make(session)
    result = await runner.run_action(Action(kind="youtube_play", target="lofi"))
    assert result.ok, result.observation
    assert session.resets == 1 and "Playing" in result.observation


async def test_window_closed_twice_gives_a_plain_message(browser: Browser) -> None:
    session = ClosingSession(await browser.new_context(), closes=10)
    runner, *_ = make(session)
    result = await runner.run_action(Action(kind="youtube_play", target="lofi"))
    assert result.error == "browser_closed" and "couldn't be reopened" in result.observation


def test_closed_errors_are_recognised() -> None:
    from jarvis.executors.browser import is_closed_error

    class TargetClosedError(Exception): ...

    assert is_closed_error(TargetClosedError("x"))
    assert is_closed_error(
        Exception("Page.evaluate: Target page, context or browser has been closed")
    )
    assert not is_closed_error(Exception("net::ERR_NAME_NOT_RESOLVED"))


async def test_invisible_captcha_helper_is_not_a_human_check(session: HeadlessSession) -> None:
    confirmer = WaitingConfirmer()
    runner, *_ = make(session, confirmer=confirmer)
    result = await runner.run_action(Action(kind="open_url", target=url("invisible-captcha.html")))
    assert result.ok, result.observation
    assert confirmer.prompts == []  # nothing to hand over: the user sees no check


async def test_page_commands_scroll_the_current_tab(session: HeadlessSession) -> None:
    runner, *_ = make(session)
    page = await session.new_page()
    await page.goto(url("long.html"))
    scroll_y = "() => window.scrollY"
    down = await runner.run_action(Action(kind="browser_nav", target="scroll_down"))
    assert down.ok and down.observation == "Scrolled down" and await page.evaluate(scroll_y) > 0
    await runner.run_action(Action(kind="browser_nav", target="scroll_bottom"))
    assert await page.locator("#end").is_visible()
    await runner.run_action(Action(kind="browser_nav", target="scroll_top"))
    assert await page.evaluate(scroll_y) == 0
