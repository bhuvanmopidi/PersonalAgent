"""YouTube: search → first real video → play → verify it's playing. Ads: ask / skip / play.

Selectors and shortcuts come from config/apps/youtube.yaml.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote_plus

import yaml
from pydantic import BaseModel, Field

from jarvis.config import CONFIG_DIR
from jarvis.models import ActionResult
from jarvis.safety.confirm import Confirmer, can_cancel
from jarvis.telemetry.logging import get_logger

if TYPE_CHECKING:
    from playwright.async_api import Page

log = get_logger(__name__)

YOUTUBE_PATH = CONFIG_DIR / "apps" / "youtube.yaml"
POLL_S = 0.25
UNSKIPPABLE_AFTER_S = 7  # an ad with no Skip button after this long won't get one
SKIP_CLICK_TRIES = 3

AdPolicy = Literal["ask", "skip", "play"]


class Selectors(BaseModel):
    result: str = "ytd-video-renderer"
    result_link: str = "a#video-title"
    player: str = "#movie_player"
    ad_showing_class: str = "ad-showing"
    skip_button: str = ".ytp-skip-ad-button, .ytp-ad-skip-button-modern, .ytp-ad-skip-button"
    duration: str = "ytd-thumbnail-overlay-time-status-renderer"  # "3:25" on the thumbnail
    video: str = "video"


class Shortcuts(BaseModel):
    next: str = "Shift+N"
    fullscreen: str = "f"


class Timeouts(BaseModel):
    results: float = 10
    start: float = 15
    ads_max: float = 120


class ErrorPage(BaseModel):
    match: str  # substring of the URL
    message: str  # what Jarvis tells you


class YouTubePageError(Exception):
    """YouTube sent the tab to an error / sign-in / consent page instead of what we asked for."""


class YouTubeConfig(BaseModel):
    search_url: str = "https://www.youtube.com/results?search_query={query}"
    ads: AdPolicy = "ask"
    ask_timeout_s: float = 8
    selectors: Selectors = Selectors()
    skip_inside: list[str] = Field(default_factory=list)
    skip_href: list[str] = Field(default_factory=list)
    skip_badges: list[str] = Field(default_factory=list)
    # live streams / premieres show no duration ("29:33") — require one (relaxed if none load)
    require_duration: bool = True
    shortcuts: Shortcuts = Shortcuts()
    timeouts_s: Timeouts = Timeouts()
    error_pages: list[ErrorPage] = Field(default_factory=list)

    def search(self, query: str) -> str:
        return self.search_url.format(query=quote_plus(query))

    def check_error_page(self, url: str) -> str | None:
        """The message for a known YouTube error/redirect page, else None."""
        return next((e.message for e in self.error_pages if e.match in url), None)


def load_youtube_config(path: Path = YOUTUBE_PATH) -> YouTubeConfig:
    if not path.exists():
        return YouTubeConfig()
    return YouTubeConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


class Candidate(BaseModel):
    index: int
    title: str
    href: str
    skipped_container: bool  # inside an ad / Shorts shelf
    badges: list[str]
    duration: str = ""  # "3:25", "1:02:03"; empty for live streams


def pick_video(
    candidates: list[Candidate], cfg: YouTubeConfig, need_duration: bool | None = None
) -> Candidate | None:
    """First result that is a plain, playable video (not an ad, Short, playlist/mix or live)."""
    need_duration = cfg.require_duration if need_duration is None else need_duration
    bad_badges = {b.lower() for b in cfg.skip_badges}
    for c in candidates:
        if not c.title or not c.href or c.skipped_container:
            continue
        if any(s in c.href for s in cfg.skip_href):
            continue
        if any(b.strip().lower() in bad_badges for b in c.badges):
            continue
        if need_duration and not _DURATION.search(c.duration):
            continue  # live stream (YouTube doesn't always show a LIVE badge)
        return c
    return None


_DURATION = re.compile(r"\d+:\d{2}")
DURATION_RETRIES = 3  # duration labels render a moment after the results


_CANDIDATES_JS = """([sel, skipInside]) =>
  [...document.querySelectorAll(sel.result)].map((el, index) => {
    const link = el.querySelector(sel.result_link);
    return {
      index,
      title: link ? (link.getAttribute('title') || link.textContent || '').trim() : '',
      href: link ? (link.getAttribute('href') || '') : '',
      skipped_container: skipInside.some(s => el.closest(s)),
      badges: [...el.querySelectorAll('.badge, ytd-badge-supported-renderer')]
        .map(b => (b.textContent || '').trim()).filter(Boolean),
      duration: ((el.querySelector(sel.duration) || {}).textContent || '').trim(),
    };
  })"""

_STATE_JS = """(sel) => {
  const player = document.querySelector(sel.player);
  const ad = !!player && player.classList.contains(sel.ad_showing_class);
  const skip = document.querySelector(sel.skip_button);
  const skippable = !!skip && skip.offsetParent !== null;
  const v = document.querySelector(sel.video);
  const playing = !!v && !v.paused && !v.ended && v.currentTime > 0;
  return {ad, skippable, playing};
}"""

_SET_MUTED_JS = """([sel, muted]) => {
  const v = document.querySelector(sel.video);
  if (!v) return null;
  const before = v.muted; v.muted = muted; return before;
}"""

_PLAY_JS = "(sel) => { const v = document.querySelector(sel.video); if (v) v.play(); }"

Announce = Callable[[str], Awaitable[None]]


async def _silent(_: str) -> None:
    return None


class YouTubeFlow:
    def __init__(
        self,
        cfg: YouTubeConfig,
        asker: Confirmer,
        announce: Announce = _silent,
    ) -> None:
        self.cfg, self.asker, self.announce = cfg, asker, announce
        self.ad_policy: AdPolicy = cfg.ads  # changed per session by "always skip ads" etc.

    def raise_if_error_page(self, page: Page) -> None:
        message = self.cfg.check_error_page(page.url)
        if message:
            raise YouTubePageError(message)

    async def wait_for_results(self, page: Page) -> None:
        """Results, or fail fast if YouTube redirected to an error / sign-in / consent page."""
        deadline = time.monotonic() + self.cfg.timeouts_s.results
        while True:
            self.raise_if_error_page(page)
            if await page.query_selector(self.cfg.selectors.result):
                return
            if time.monotonic() > deadline:
                raise YouTubePageError("YouTube didn't show any results.")
            await asyncio.sleep(0.2)

    async def pick_and_click(self, page: Page) -> Candidate | None:
        sel = self.cfg.selectors
        await self.wait_for_results(page)
        choice = None
        for attempt in range(DURATION_RETRIES + 1):
            raw = await page.evaluate(_CANDIDATES_JS, [sel.model_dump(), self.cfg.skip_inside])
            candidates = [Candidate.model_validate(r) for r in raw]
            # last attempt: don't insist on a duration (labels may never load) rather than fail
            choice = pick_video(candidates, self.cfg, None if attempt < DURATION_RETRIES else False)
            if choice is not None:
                break
            await asyncio.sleep(0.4)
        if choice is None:
            return None
        link = page.locator(sel.result).nth(choice.index).locator(sel.result_link).first
        await link.click()
        return choice

    async def until_playing(self, page: Page, title: str) -> ActionResult:
        """Wait for the video itself to play, dealing with ads on the way."""
        sel = self.cfg.selectors.model_dump()
        content_waited = 0.0  # time without an ad — ads don't count against the start timeout
        ad_since: float | None = None
        ads_seconds = 0.0  # total time spent in ads, reported in the result
        skip: bool | None = None  # this ad: None = not decided yet
        clicks = 0
        announced = nudged = False
        started = time.monotonic()
        while True:
            self.raise_if_error_page(page)
            state = await page.evaluate(_STATE_JS, sel)
            if state["ad"]:
                if ad_since is None:
                    ad_since = time.monotonic()
                    log.info("ad started")
                if state["skippable"] and skip is None:
                    skip = await self._should_skip(page)
                    log.info(
                        "ad skippable after %.0f s — skip: %s", time.monotonic() - ad_since, skip
                    )
                if skip and state["skippable"] and clicks < SKIP_CLICK_TRIES:
                    clicks += 1  # retried: a click can miss if the button re-renders
                    try:
                        await page.click(self.cfg.selectors.skip_button, timeout=2000)
                    except Exception:  # the ad ended while we were asking
                        log.debug("skip button gone", exc_info=True)
                elif not state["skippable"] and not announced:
                    if time.monotonic() - ad_since > UNSKIPPABLE_AFTER_S:
                        announced = True
                        log.info("ad can't be skipped")
                        await self.announce("An ad is playing. It can't be skipped.")
                if time.monotonic() - started > self.cfg.timeouts_s.ads_max:
                    return ActionResult(ok=True, observation=f"Opened {title} (an ad is still on)")
            else:
                if ad_since is not None:  # an ad just ended (the next one in a pod is new)
                    ads_seconds += time.monotonic() - ad_since
                    log.info(
                        "ad ended after %.0f s (skip clicks: %d)",
                        time.monotonic() - ad_since,
                        clicks,
                    )
                ad_since, skip, clicks, announced = None, None, 0, False
                if state["playing"]:
                    after = f" (after {ads_seconds:.0f} s of ads)" if ads_seconds >= 1 else ""
                    return ActionResult(ok=True, observation=f"Playing: {title}{after}")
                content_waited += POLL_S
                if content_waited > 5 and not nudged:  # autoplay blocked? press play once
                    nudged = True
                    await page.evaluate(_PLAY_JS, sel)
                if content_waited > self.cfg.timeouts_s.start:
                    return ActionResult(
                        ok=False,
                        error="not_playing",
                        observation=f"Opened {title}, but it didn't start",
                    )
            await asyncio.sleep(POLL_S)

    async def _should_skip(self, page: Page) -> bool:
        if self.ad_policy != "ask":
            return self.ad_policy == "skip"
        sel = self.cfg.selectors.model_dump()
        was_muted = await page.evaluate(
            _SET_MUTED_JS, [sel, True]
        )  # so the mic hears you, not the ad
        try:
            question = self.asker.confirm("There's an ad. Skip it?")
            if not can_cancel(self.asker):
                return await question
            try:
                return await asyncio.wait_for(question, self.cfg.ask_timeout_s)
            except TimeoutError:
                log.info("no answer about the ad — letting it play")
                return False
        finally:
            if was_muted is not None:
                await page.evaluate(_SET_MUTED_JS, [sel, bool(was_muted)])
