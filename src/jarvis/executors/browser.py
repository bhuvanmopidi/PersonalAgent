"""Browser actions in the Jarvis Chrome window. Only ever called through ActionRunner.

Kinds: open_url · youtube_play · media_key (pause/resume/next/fullscreen/skip_ad) ·
browser_nav (back/forward/reload/new_tab/close_tab) · set_ad_policy.
Every website opens here. If the Jarvis Chrome window can't be started the action fails with
a clear message; the default browser is used only when browser.enabled is false.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from jarvis.apps.media_time import fmt_clock, parse_clock, to_seconds
from jarvis.apps.youtube_flow import AdPolicy, YouTubeConfig, YouTubeFlow, YouTubePageError
from jarvis.executors.browser_session import BrowserUnavailable, PageSource, pick_active
from jarvis.models import Action, ActionResult
from jarvis.safety.captcha import CaptchaGate
from jarvis.safety.confirm import Confirmer
from jarvis.safety.policy import CaptchaRules
from jarvis.telemetry.logging import get_logger

if TYPE_CHECKING:
    from playwright.async_api import Page

log = get_logger(__name__)

BROWSER_KINDS = frozenset({"open_url", "youtube_play", "media_key", "browser_nav", "set_ad_policy"})
PAGE_KINDS = frozenset({"media_key", "browser_nav"})  # act on the current tab → it's page context
NAV_TIMEOUT_MS = 20_000
_BLANK = ("about:blank", "chrome://newtab", "chrome://new-tab-page", "chrome-search://")
_AD_POLICIES: dict[str, AdPolicy] = {"always": "skip", "never": "play", "ask": "ask"}

Announce = Callable[[str], Awaitable[None]]


def ok(observation: str) -> ActionResult:
    return ActionResult(ok=True, observation=observation)


def fail(error: str, observation: str) -> ActionResult:
    return ActionResult(ok=False, error=error, observation=observation)


_TIME_JS = """([sel, op]) => {
  const v = document.querySelector(sel);
  if (!v) return null;
  if (op.kind === 'speed') {
    const rate = op.rate !== null ? op.rate : v.playbackRate + op.step;
    v.playbackRate = Math.min(2, Math.max(0.25, rate));
    return {rate: v.playbackRate};
  }
  const d = v.duration;
  if (!isFinite(d)) return {live: true};
  const target = op.kind === 'to' ? op.to : v.currentTime + op.delta;
  v.currentTime = Math.min(Math.max(0, target), Math.max(0, d - 1));  // never past the end
  return {t: v.currentTime, d};
}"""


def _float(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def is_closed_error(exc: BaseException) -> bool:
    """The page, tab, window or Chrome itself went away (Playwright's TargetClosedError isn't
    exported, so match by name and message)."""
    if type(exc).__name__ == "TargetClosedError":
        return True
    text = str(exc).lower()
    return any(
        s in text
        for s in ("has been closed", "target closed", "browser closed", "connection closed")
    )


def is_blank(url: str) -> bool:
    return not url or url.startswith(_BLANK)


class BrowserExecutor:
    def __init__(
        self,
        session: PageSource | None,
        youtube: YouTubeConfig,
        captcha: CaptchaRules,
        asker: Confirmer,
        announce: Announce,
        opener: Callable[[str], None] = os.startfile,
    ) -> None:
        self.session = session  # None = browser automation disabled in settings
        self.youtube = YouTubeFlow(youtube, asker, announce)
        self.captcha = CaptchaGate(captcha, asker)
        self._open = opener

    async def annotate(self, action: Action) -> Action:
        """Before the safety check: actions on the current tab get that tab's URL as context."""
        if action.kind not in PAGE_KINDS or self.session is None or action.page_url:
            return action
        page = await pick_active(self.session.open_pages())
        return action.model_copy(update={"page_url": page.url}) if page else action

    async def execute(self, action: Action) -> ActionResult:
        if action.kind == "set_ad_policy":
            return self._set_ad_policy(action.target)
        from playwright.async_api import Error as PlaywrightError

        try:
            return await self._run(action)
        except PlaywrightError as exc:
            if not is_closed_error(exc) or self.session is None:
                raise
            # the Jarvis Chrome window (or its tab) was closed under us — like a person would,
            # reopen it and do the same thing again, once
            log.info("Jarvis Chrome was closed during %s — reopening and retrying", action.kind)
            await self.session.reset()
            try:
                return await self._run(action)
            except PlaywrightError as again:
                first_line = str(again).splitlines()[0]
                return fail(
                    "browser_closed",
                    f"The Jarvis Chrome window was closed and couldn't be reopened: {first_line}",
                )

    async def _run(self, action: Action) -> ActionResult:
        """One attempt. Closed-window errors propagate (execute retries); others become results."""
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeout

        try:
            if action.kind == "open_url":
                return await self.open_url(action.target)
            if action.kind == "youtube_play":
                url = self.youtube.cfg.search(action.target)
                return await self._fallback_on_error(url, lambda: self._youtube_play(action.target))
            if action.kind == "media_key":
                return await self._media(action.target, action.args)
            if action.kind == "browser_nav":
                return await self._nav(action.target)
        except BrowserUnavailable as exc:
            return fail("browser_unavailable", f"I can't reach the Jarvis Chrome window: {exc}")
        except PlaywrightTimeout:
            return fail("timeout", "The page took too long.")
        except PlaywrightError as exc:
            if is_closed_error(exc):
                raise
            first_line = str(exc).splitlines()[0]
            log.warning("browser error during %s: %s", action.kind, first_line)  # one line
            log.debug("browser error details", exc_info=True)  # traceback only with debug logs
            return fail("browser_error", f"Chrome error: {first_line}")
        return fail("unsupported", f"Browser executor can't do '{action.kind}'")

    # ---- opening pages ----------------------------------------------------------------------

    async def open_url(self, url: str) -> ActionResult:
        """Also used by launch_app when "open X" resolves to a website."""
        return await self._fallback_on_error(url, lambda: self._open_url(url))

    async def _open_url(self, url: str) -> ActionResult:
        page = await self._tab_for_new_site()
        await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        await page.bring_to_front()
        blocked = await self.captcha.check(page)
        return blocked or ok(f"Opened {url}")

    async def _youtube_play(self, query: str) -> ActionResult:
        page = await self._youtube_tab()
        await page.goto(self.youtube.cfg.search(query), wait_until="domcontentloaded",
                        timeout=NAV_TIMEOUT_MS)  # fmt: skip
        await page.bring_to_front()
        blocked = await self.captcha.check(page)
        if blocked:
            return blocked
        try:
            choice = await self.youtube.pick_and_click(page)
            if choice is None:
                return fail("no_results", f"I couldn't find a video for '{query}'")
            blocked = await self.captcha.check(page)
            return blocked or await self.youtube.until_playing(page, choice.title)
        except YouTubePageError as exc:  # the window is already in front, so you can see it
            log.warning("youtube page problem at %s: %s", page.url, exc)
            return fail("youtube_error", str(exc))

    async def _fallback_on_error(
        self, url: str, attempt: Callable[[], Awaitable[ActionResult]]
    ) -> ActionResult:
        """Websites always open in the Jarvis Chrome window. The default browser is used only when
        browser automation is switched off in settings — never silently when Chrome fails."""
        if self.session is None:
            self._open(url)
            return ok(f"Opened {url} in your default browser")
        try:
            return await attempt()
        except BrowserUnavailable as exc:
            log.warning("browser unavailable: %s", exc)
            return fail("browser_unavailable", f"I couldn't open the Jarvis Chrome window: {exc}")

    async def _tab_for_new_site(self) -> Page:
        """Reuse the current tab if it's empty, otherwise open a new one (like a person would)."""
        assert self.session is not None
        page = await pick_active(await self.session.pages())
        if page is not None and is_blank(page.url):
            return page
        return await self.session.new_page()

    async def _youtube_tab(self) -> Page:
        assert self.session is not None
        pages = await self.session.pages()
        youtube = [p for p in pages if "youtube.com" in p.url]
        if youtube:
            return await pick_active(youtube) or youtube[-1]
        return await self._tab_for_new_site()

    # ---- the current tab --------------------------------------------------------------------

    async def _media(self, key: str, args: dict[str, Any] | None = None) -> ActionResult:
        page = await self._video_tab()
        if page is None:
            return fail("nothing_playing", "There's no video open in Chrome.")
        sel = self.youtube.cfg.selectors
        if key in ("seek", "seek_to", "restart", "speed"):
            return await self._time_control(page, key, args or {})
        if key in ("pause", "resume"):
            js = "([s, m]) => { const v = document.querySelector(s); if (v) v[m](); return !!v; }"
            found = await page.evaluate(js, [sel.video, "pause" if key == "pause" else "play"])
            if not found:
                return fail("nothing_playing", "There's no video on this page.")
            return ok("Paused" if key == "pause" else "Playing")
        if key == "skip_ad":
            skip = page.locator(sel.skip_button).first
            if not await skip.is_visible():
                return fail("no_ad", "There's no ad to skip right now.")
            await skip.click()
            return ok("Skipped the ad")
        if not await page.evaluate("(s) => !!document.querySelector(s)", sel.video):
            return fail("nothing_playing", "There's no video on this page.")
        shortcut = {"next": self.youtube.cfg.shortcuts.next,
                    "fullscreen": self.youtube.cfg.shortcuts.fullscreen}.get(key)  # fmt: skip
        if shortcut is None:
            return fail("unsupported", f"Unknown media key '{key}'")
        await page.bring_to_front()
        # YouTube shortcuts are ignored while a text box has focus
        await page.evaluate("() => document.activeElement && document.activeElement.blur()")
        await page.keyboard.press(shortcut)
        return ok("Next video" if key == "next" else "Toggled full screen")

    async def _time_control(self, page: Page, key: str, args: dict[str, Any]) -> ActionResult:
        """Seek / restart / playback speed, by setting the <video> element directly."""
        op: dict[str, Any]
        if key == "speed":
            rate = _float(args.get("rate"))
            op = {"kind": "speed", "rate": rate, "step": _float(args.get("step")) or 0.0}
        elif key == "restart":
            op = {"kind": "to", "to": 0.0}
        elif key == "seek_to":
            to = parse_clock(str(args.get("time", "")))
            if to is None:  # not "5:30" — maybe "10 minutes"
                to = to_seconds(str(args.get("amount", "")), str(args.get("unit", "")))
            if to is None:
                return fail("bad_time", "I didn't get the time to jump to.")
            op = {"kind": "to", "to": to}
        else:  # seek
            delta = to_seconds(str(args.get("amount", "")), str(args.get("unit", "")))
            if delta is None:
                return fail("bad_time", "I didn't get how far to skip.")
            op = {"kind": "by", "delta": -delta if args.get("direction") == "back" else delta}

        state = await page.evaluate(_TIME_JS, [self.youtube.cfg.selectors.video, op])
        if state is None:
            return fail("nothing_playing", "There's no video on this page.")
        if state.get("live"):
            return fail("live_stream", "This is a live stream, so I can't skip around in it.")
        if "rate" in state:
            return ok(f"Speed {state['rate']:g}x")
        return ok(f"At {fmt_clock(state['t'])} of {fmt_clock(state['d'])}")

    async def _video_tab(self) -> Page | None:
        assert self.session is not None
        pages = await self.session.pages()
        watching = [p for p in pages if "youtube.com/watch" in p.url]
        if watching:
            return await pick_active(watching)
        return await pick_active(pages)

    async def _nav(self, where: str) -> ActionResult:
        assert self.session is not None
        if where == "new_tab":
            tab = await self.session.new_page()
            await tab.bring_to_front()
            return ok("New tab")
        page = await pick_active(await self.session.pages())
        if page is None:
            return fail("no_tab", "There's no tab open.")
        if where == "back":
            await page.go_back(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            return ok("Went back")
        if where == "forward":
            await page.go_forward(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            return ok("Went forward")
        if where == "reload":
            await page.reload(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            return ok("Reloaded")
        if where == "close_tab":
            await page.close()
            return ok("Closed the tab")
        return fail("unsupported", f"Unknown navigation '{where}'")

    def _set_ad_policy(self, value: str) -> ActionResult:
        policy = _AD_POLICIES.get(value)
        if policy is None:
            return fail("unsupported", f"Unknown ad setting '{value}'")
        self.youtube.ad_policy = policy
        return ok({"skip": "I'll skip ads", "play": "I'll leave ads alone",
                   "ask": "I'll ask about ads"}[policy])  # fmt: skip
