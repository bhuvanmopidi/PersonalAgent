"""Browser actions in the Jarvis Chrome window. Only ever called through ActionRunner.

Kinds: open_url · youtube_play · media_key (pause/resume/next/fullscreen/skip_ad) ·
browser_nav (back/forward/reload/new_tab/close_tab/scroll_down/scroll_up/scroll_top/
scroll_bottom) · set_ad_policy · and the general agent's page actions (Phase 11): click · type_text
· press_key · select_option · scroll · find · navigate · go_back · wait — on any site, by the
element number from the agent's page snapshot.
Every website opens here. If the Jarvis Chrome window can't be started the action fails with
a clear message; the default browser is used only when browser.enabled is false.
"""

from __future__ import annotations

import contextlib
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
    from playwright.async_api import Locator, Page

log = get_logger(__name__)

AGENT_KINDS = frozenset(
    {"click", "type_text", "press_key", "select_option", "scroll", "find", "navigate", "go_back",
     "wait"}
)  # fmt: skip
BROWSER_KINDS = frozenset(
    {"open_url", "youtube_play", "media_key", "browser_nav", "set_ad_policy", *AGENT_KINDS}
)
ELEMENT_TIMEOUT_MS = 3_000

# What is on top of the element's centre, if not the element itself (a popup / overlay / dropdown)?
_COVERED_BY_JS = """el => {
  const r = el.getBoundingClientRect();
  if (r.width < 1 || r.height < 1) return 'nothing visible (it has no size)';
  const x = Math.min(Math.max(r.left + r.width / 2, 0), innerWidth - 1);
  const y = Math.min(Math.max(r.top + r.height / 2, 0), innerHeight - 1);
  const top = document.elementFromPoint(x, y);
  if (!top || top === el || el.contains(top) || top.contains(el)) return null;
  const label = top.closest('label');
  if (label && (label.control === el || label.contains(el))) return null;
  // describe the covering thing by its nearest container with some text
  let box = top;
  for (let i = 0; i < 6 && box.parentElement && (box.innerText || '').trim().length < 3; i++)
    box = box.parentElement;
  const text = (box.getAttribute('aria-label') || box.innerText || '').replace(/\\s+/g, ' ').trim();
  return (box.tagName.toLowerCase() + (text ? ' "' + text.slice(0, 60) + '"' : ''));
}"""
# Ctrl+F for the agent: search the WHOLE page (not just what's on screen) for some words, scroll
# the first visible match into view. Text, plus labels/placeholders of fields and buttons.
_FIND_JS = r"""(q) => {
  const want = q.toLowerCase().replace(/\s+/g, ' ').trim();
  const seen = el => { const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const hits = [];
  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = walk.nextNode(); n; n = walk.nextNode())
    if (n.textContent.toLowerCase().replace(/\s+/g, ' ').includes(want) && n.parentElement)
      hits.push(n.parentElement);
  for (const el of document.querySelectorAll('[aria-label], [placeholder], [title]')) {
    const label = ['aria-label', 'placeholder', 'title']
      .map(a => el.getAttribute(a) || '').join(' ');
    if (label.toLowerCase().includes(want)) hits.push(el);
  }
  const shown = hits.filter(seen);
  if (!shown.length) return {count: 0};
  const first = shown[0];
  first.scrollIntoView({block: 'center'});
  const around = (first.innerText || first.getAttribute('aria-label') || '').replace(/\s+/g, ' ');
  return {count: shown.length, around: around.trim().slice(0, 80)};
}"""
_SCROLL_JS = """(where) => {
  if (where === 'scroll_top') window.scrollTo(0, 0);
  else if (where === 'scroll_bottom') window.scrollTo(0, document.documentElement.scrollHeight);
  else window.scrollBy(0, (where === 'scroll_up' ? -0.8 : 0.8) * innerHeight);
}"""
_SCROLLED = {
    "scroll_down": "Scrolled down",
    "scroll_up": "Scrolled up",
    "scroll_top": "At the top of the page",
    "scroll_bottom": "At the bottom of the page",
}
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


async def _settle(page: Page) -> None:
    """After a click / key press: let a navigation start and the DOM load (event-based, capped)."""
    # still loading after 3 s, or a single-page app that never "loads": fine either way
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("domcontentloaded", timeout=3_000)
    await page.wait_for_timeout(300)  # let single-page apps render the change


def _why(exc: BaseException) -> str:
    """The useful part of a Playwright timeout: what got in the way."""
    for line in str(exc).splitlines():
        if "intercepts pointer events" in line:
            return "something on top of it is in the way (" + line.strip(" -")[:90] + ")"
        if "not visible" in line or "not enabled" in line or "not stable" in line:
            return "it is " + line.strip(" -").split("element is ")[-1][:40]
    return "it didn't respond in time (hidden, covered or gone)"


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
        self.agent_page: Page | None = None  # the tab the general agent is working in
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
            if action.kind in AGENT_KINDS:
                return await self._agent_op(action)
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

    # ---- the general agent's page actions (any website) -------------------------------------

    async def agent_tab(self) -> Page:
        """The tab the agent works in: the one it used last if still open, else the active tab
        (a new one if there is none). Brought to the front so the user sees what happens."""
        if self.session is None:
            raise BrowserUnavailable("browser automation is switched off in settings")
        page = self.agent_page
        if page is None or page.is_closed():
            page = await pick_active(await self.session.pages()) or await self.session.new_page()
            self.agent_page = page
        await page.bring_to_front()
        return page

    async def _agent_op(self, action: Action) -> ActionResult:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeout

        from jarvis.tiers.structured.observer import locator_for

        page = await self.agent_tab()
        a = action.args
        element = a.get("element")
        loc = page.locator(locator_for(int(element))).first if element is not None else None
        what = f'"{action.target}"' if action.target else f"[{element}]"
        try:
            if action.kind == "click":
                if loc is None:
                    return fail("bad_element", "click needs an element")
                blocked = await self._covered(loc, what)
                if blocked is not None:
                    return blocked
                await loc.click(timeout=ELEMENT_TIMEOUT_MS)
                await _settle(page)
                return ok(f"Clicked {what}")
            if action.kind == "type_text":
                if loc is None:
                    return fail("bad_element", "type needs an element")
                text = str(a.get("text", ""))
                try:
                    await loc.fill(text, timeout=ELEMENT_TIMEOUT_MS)
                except PlaywrightError as exc:
                    if "not an <input>" not in str(exc) and "not editable" not in str(exc):
                        raise
                    # a custom field (a styled box, not a real text field): do what a person does
                    blocked = await self._covered(loc, what)
                    if blocked is not None:
                        return blocked
                    await loc.click(timeout=ELEMENT_TIMEOUT_MS)
                    await page.keyboard.press("Control+A")
                    await page.keyboard.type(text, delay=20)
                    await _settle(page)
                return ok(f"Typed into {what}")
            if action.kind == "press_key":
                key = str(a.get("key") or "Enter")
                if loc is not None:
                    await loc.press(key, timeout=ELEMENT_TIMEOUT_MS)
                else:
                    await page.keyboard.press(key)
                await _settle(page)
                return ok(f"Pressed {key}")
            if action.kind == "select_option":
                if loc is None:
                    return fail("bad_element", "select needs an element")
                await loc.select_option(label=str(a.get("text", "")), timeout=ELEMENT_TIMEOUT_MS)
                return ok(f"Selected {a.get('text')!r} in {what}")
            if action.kind == "scroll":
                up = str(a.get("direction", "down")).lower() == "up"
                await page.evaluate(
                    "(up) => window.scrollBy(0, (up ? -0.8 : 0.8) * innerHeight)", up
                )
                return ok(f"Scrolled {'up' if up else 'down'}")
            if action.kind == "find":
                words = str(a.get("text", "")).strip()
                if not words:
                    return fail("bad_find", "find needs the words to look for")
                found = await page.evaluate(_FIND_JS, words)
                if not found["count"]:
                    return fail("not_found", f'"{words}" is not anywhere on this page')
                more = f" ({found['count']} matches)" if found["count"] > 1 else ""
                return ok(f'Found "{words}"{more} — scrolled to it: "{found["around"]}"')
            if action.kind == "navigate":
                await page.goto(
                    action.target, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS
                )
                blocked = await self.captcha.check(page)
                return blocked or ok(f"Opened {page.url}")
            if action.kind == "go_back":
                await page.go_back(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                return ok("Went back")
            # wait
            await page.wait_for_timeout(min(float(a.get("seconds", 1.0)), 5.0) * 1000)
            await _settle(page)
            return ok("Waited")
        except PlaywrightTimeout as exc:
            return fail("element_not_ready", f"Couldn't {action.kind.replace('_', ' ')} {what}: "
                        f"{_why(exc)}. Look at the page again")  # fmt: skip

    async def _covered(self, loc: Locator, what: str) -> ActionResult | None:
        """Fail at once — with the reason — if something covers the element (no 3 s wait)."""
        try:
            await loc.scroll_into_view_if_needed(timeout=ELEMENT_TIMEOUT_MS)
            cover = await loc.evaluate(_COVERED_BY_JS)
        except Exception:
            return None  # let the click itself report what's wrong
        if not cover:
            return None
        return fail("covered", f"{what} is covered by {cover} — finish or close that first "
                    "(e.g. Done / Apply / Close, or press Escape)")  # fmt: skip

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
        if where in _SCROLLED:
            await page.evaluate(_SCROLL_JS, where)
            return ok(_SCROLLED[where])
        return fail("unsupported", f"Unknown navigation '{where}'")

    def _set_ad_policy(self, value: str) -> ActionResult:
        policy = _AD_POLICIES.get(value)
        if policy is None:
            return fail("unsupported", f"Unknown ad setting '{value}'")
        self.youtube.ad_policy = policy
        return ok({"skip": "I'll skip ads", "play": "I'll leave ads alone",
                   "ask": "I'll ask about ads"}[policy])  # fmt: skip
