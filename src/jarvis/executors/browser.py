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

import asyncio
import contextlib
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
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
    from playwright.async_api import Dialog, Download, Locator, Page

log = get_logger(__name__)

AGENT_KINDS = frozenset(
    {"click", "double_click", "type_text", "press_key", "select_option", "scroll", "find",
     "navigate", "go_back", "wait", "attach_file", "open_file", "wait_download",
     "tab_open", "tab_switch", "tab_close", "private_window", "click_at", "type_at"}
)  # fmt: skip
MAX_TABS_SHOWN = 8
_LINK_JS = "el => { const a = el.closest('a[href]'); return a ? a.href : ''; }"
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
# What scrolls here? Many web apps (chat apps, mail, docs) scroll an inner panel, not the page.
# Like a person: the panel under the middle of the screen (or the biggest one), else the page.
# Marks it, and returns where it is and how far it has scrolled.
_SCROLLER_JS = """() => {
  const scrolls = e => {
    const s = getComputedStyle(e);
    return /(auto|scroll|overlay)/.test(s.overflowY) && e.scrollHeight > e.clientHeight + 4;
  };
  let pick = null;
  for (let e = document.elementFromPoint(innerWidth / 2, innerHeight / 2);
       e && e !== document.body && e !== document.documentElement; e = e.parentElement)
    if (scrolls(e)) { pick = e; break; }
  const page = document.scrollingElement || document.documentElement;
  if (!pick && page.scrollHeight <= page.clientHeight + 4) {
    let area = 0;
    for (const e of document.querySelectorAll('div, main, section, article, ul, ol')) {
      if (!scrolls(e)) continue;
      const r = e.getBoundingClientRect(), a = r.width * r.height;
      if (a > area) { area = a; pick = e; }
    }
  }
  for (const e of document.querySelectorAll('[data-jarvis-scroll]'))
    e.removeAttribute('data-jarvis-scroll');
  const el = pick || page;
  el.setAttribute('data-jarvis-scroll', '1');
  const r = pick ? pick.getBoundingClientRect()
    : {left: 0, top: 0, width: innerWidth, height: innerHeight};
  return {x: r.left + r.width / 2, y: r.top + r.height / 2, top: el.scrollTop,
          height: el.clientHeight, max: el.scrollHeight - el.clientHeight, inner: !!pick};
}"""
_SCROLL_POS_JS = """() => {
  const e = document.querySelector('[data-jarvis-scroll]');
  return e ? e.scrollTop : 0;
}"""
_SCROLL_END_JS = """(top) => { const e = document.querySelector('[data-jarvis-scroll]');
  if (e) e.scrollTo({top: top ? 0 : e.scrollHeight}); }"""
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
        self.downloads_dir: Path | None = (
            None  # None = the user's Downloads folder (evals: a temp one)
        )
        self.downloads: list[Path] = []  # files downloaded in the agent's tab, newest last
        self._watched: set[int] = set()  # tabs whose downloads we save
        self._reported = 0  # downloads already handed to the agent
        self._opened: set[int] = set()  # tabs Jarvis opened (new tabs, followed pop-ups, private)
        self._saving = 0  # downloads still being written
        self._last_step = time.time()  # when the agent's previous action started
        self.youtube = YouTubeFlow(youtube, asker, announce)
        self._asker = asker  # the page's own questions (confirm / prompt boxes) go to the user
        self._dialog_notes: list[str] = []  # what the page asked / said during the last step
        self._dialogs_open = 0  # page questions waiting for the user's answer
        self.captcha = CaptchaGate(captcha, asker)
        self._open = opener

    async def annotate(self, action: Action) -> Action:
        """Before the safety check: actions on the current tab get that tab's URL as context; a
        tab the agent would close that Jarvis didn't open gets flagged (the user's own tab)."""
        if action.kind == "tab_close":
            tab = self._find_tab(action.target) if action.target else self.agent_page
            if tab is not None and id(tab) not in self._opened:
                return action.model_copy(update={"flags": action.flags | {"users_tab"}})
            return action
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
        if id(page) not in self._watched:  # keep what this tab downloads, in the Downloads folder
            self._watched.add(id(page))
            page.on("download", self._on_download)
            page.on("dialog", self._on_dialog)
        await page.bring_to_front()
        return page

    def _on_dialog(self, dialog: Dialog) -> None:
        self._dialogs_open += 1
        asyncio.get_running_loop().create_task(self._answer_dialog(dialog))

    async def _answer_dialog(self, dialog: Dialog) -> None:
        """The page's own pop-up box (alert / confirm / prompt / "leave this page?"). Left alone,
        the browser library answers Cancel — e.g. Gmail's "Send without a subject?" then silently
        doesn't send. A notice is acknowledged; a question goes to the user."""
        message = " ".join(dialog.message.split())[:200] or "(no text)"
        try:
            if dialog.type == "alert":
                self._dialog_notes.append(f'the page said: "{message}"')
                await dialog.accept()
                return
            question = f'The page asks: "{message}" — OK?'
            if dialog.type == "beforeunload":
                question = "The page warns you may lose changes if it closes or moves on — OK?"
            yes = await self._asker.confirm(question)
            self._dialog_notes.append(
                f'the page asked "{message}" — the user said {"OK" if yes else "Cancel"}'
            )
            if yes:
                await dialog.accept()
            else:
                await dialog.dismiss()
        except Exception:  # the page moved on meanwhile
            log.debug("page dialog went away before it was answered", exc_info=True)
        finally:
            self._dialogs_open -= 1

    def _on_download(self, download: Download) -> None:
        self._saving += 1
        asyncio.get_running_loop().create_task(self._save_download(download))

    async def _save_download(self, download: Download) -> None:
        from jarvis.executors.files import downloads_folder

        folder = self.downloads_dir or downloads_folder()
        name = download.suggested_filename or "download"
        target, n = folder / name, 1
        while target.exists():  # like the browser: "report (1).pdf"
            target = folder / f"{Path(name).stem} ({n}){Path(name).suffix}"
            n += 1
        try:
            await download.save_as(target)
            self.downloads.append(target)
            log.info("download saved to %s", target)
        except Exception:
            log.warning("download %s couldn't be saved", name, exc_info=True)
        finally:
            self._saving -= 1

    async def visible_text(self) -> str:
        """The visible text of the agent's tab (no hidden / off-page text), for the order reader."""
        from jarvis.safety.checkout import MAX_PAGE_CHARS, VISIBLE_TEXT_JS

        page = await self.agent_tab()
        return str(await page.evaluate(VISIBLE_TEXT_JS, MAX_PAGE_CHARS * 2))

    async def _agent_op(self, action: Action) -> ActionResult:
        """One agent step — plus what the page asked or said meanwhile (its own pop-up boxes)."""
        self._dialog_notes = []
        result = await self._agent_step(action)
        deadline = time.monotonic() + 300  # the user may take a moment to answer the page
        while self._dialogs_open and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        if self._dialog_notes:
            notes = "; ".join(self._dialog_notes)
            result = result.model_copy(update={
                "observation": f"{result.observation} ({notes})",
                "data": {**result.data, "page_notes": notes},
            })  # fmt: skip
        return result

    async def _agent_step(self, action: Action) -> ActionResult:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeout

        from jarvis.tiers.structured.observer import locator_for

        page = await self.agent_tab()
        if action.kind != "wait_download":
            self._last_step = time.time()
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
                before = self._tab_ids(page)
                await loc.click(timeout=ELEMENT_TIMEOUT_MS)  # the site decides: same tab or new
                await _settle(page)
                return ok(f"Clicked {what}{await self._follow_new_tab(page, before)}")
            if action.kind == "double_click":
                if loc is None:
                    return fail("bad_element", "double click needs an element")
                blocked = await self._covered(loc, what)
                if blocked is not None:
                    return blocked
                before = self._tab_ids(page)
                await loc.dblclick(timeout=ELEMENT_TIMEOUT_MS)
                await _settle(page)
                return ok(f"Double-clicked {what}{await self._follow_new_tab(page, before)}")
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
                before = self._tab_ids(page)
                if loc is not None:
                    await loc.press(key, timeout=ELEMENT_TIMEOUT_MS)
                else:
                    await page.keyboard.press(key)
                await _settle(page)
                return ok(f"Pressed {key}{await self._follow_new_tab(page, before)}")
            if action.kind in ("tab_open", "tab_switch", "tab_close", "private_window"):
                return await self._tabs_op(page, loc, action, what)
            if action.kind in ("click_at", "type_at"):  # a box picked on a screenshot
                before = self._tab_ids(page)
                await page.mouse.click(float(a["x"]), float(a["y"]))
                if action.kind == "type_at":
                    await page.keyboard.type(str(a.get("text", "")), delay=20)
                await _settle(page)
                return ok(f"Clicked {what}{await self._follow_new_tab(page, before)}")
            if action.kind == "select_option":
                if loc is None:
                    return fail("bad_element", "select needs an element")
                wanted = str(a.get("text", "")).strip()
                choices = [c.strip() for c in await loc.evaluate(
                    "el => el.options ? [...el.options].map(o => o.text) : []")]  # fmt: skip
                exact = [c for c in choices if c.lower() == wanted.lower()]
                partial = [c for c in choices if wanted.lower() in c.lower()]
                pick = exact or partial
                if choices and len(pick) != 1:
                    listed = ", ".join(pick or choices)[:200]
                    why = "several options match" if pick else "no option matches"
                    return fail("ambiguous_option" if pick else "no_option",
                                f'{why} "{wanted}" in {what}: {listed}')  # fmt: skip
                await loc.select_option(label=pick[0] if pick else wanted,
                                        timeout=ELEMENT_TIMEOUT_MS)  # fmt: skip
                return ok(f"Selected {pick[0] if pick else wanted!r} in {what}")
            if action.kind == "scroll":
                up = str(a.get("direction", "down")).lower() == "up"
                return await self._scroll(page, "scroll_up" if up else "scroll_down")
            if action.kind == "find":
                words = str(a.get("text", "")).strip()
                if not words:
                    return fail("bad_find", "find needs the words to look for")
                found = await page.evaluate(_FIND_JS, words)
                if not found["count"]:
                    return fail("not_found", f'"{words}" is not anywhere on this page')
                more = f" ({found['count']} matches)" if found["count"] > 1 else ""
                return ok(f'Found "{words}"{more} — scrolled to it: "{found["around"]}"')
            if action.kind == "attach_file":
                return await self._attach(page, loc, action, what)
            if action.kind == "open_file":
                from jarvis.executors.files import open_file

                return await open_file(action, self._open)
            if action.kind == "wait_download":
                from jarvis.executors.files import downloads_folder, wait_for_download

                folder = self.downloads_dir or downloads_folder()
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:  # what this tab downloaded (usually instant)
                    fresh = [p for p in self.downloads[self._reported :] if p.is_file()]
                    if fresh:
                        self._reported = len(self.downloads)
                        return await wait_for_download(folder, 0, known=[fresh[-1]])
                    if not self._saving:
                        break
                    await asyncio.sleep(0.25)
                # nothing from the tab (yet): a file arriving in the folder since the last step
                return await wait_for_download(folder, self._last_step - 1)
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

    async def _attach(
        self, page: Page, loc: Locator | None, action: Action, what: str
    ) -> ActionResult:
        """Like a person: click Attach / Upload and pick the file when the chooser opens — or put
        it straight into the page's file box."""
        from playwright.async_api import TimeoutError as PlaywrightTimeout

        from jarvis.executors.files import attached, resolved

        path, failure = resolved(action)
        if failure is not None or path is None:
            return failure or fail("no_file", "which file?")
        if loc is not None and await loc.evaluate(
            "e => e.tagName === 'INPUT' && e.type === 'file'"
        ):
            await loc.set_input_files(str(path))
        elif loc is not None:
            try:
                async with page.expect_file_chooser(timeout=ELEMENT_TIMEOUT_MS) as chooser:
                    await loc.click(timeout=ELEMENT_TIMEOUT_MS)
                await (await chooser.value).set_files(str(path))
            except PlaywrightTimeout:
                if not await self._only_file_box(page, path):
                    return fail("no_chooser", f"Clicking {what} didn't open a file picker")
        elif not await self._only_file_box(page, path):
            return fail("no_file_box", "There's no single upload box here — name the Attach button")
        await _settle(page)
        return attached(path)

    async def _only_file_box(self, page: Page, path: Path) -> bool:
        boxes = page.locator("input[type=file]")
        if await boxes.count() != 1:
            return False
        await boxes.first.set_input_files(str(path))
        return True

    # ---- tabs: see them, follow new ones, open / switch / close when asked -------------------

    def _all_tabs(self) -> list[Page]:
        """Every open Jarvis Chrome tab (the private window's too), oldest first."""
        tabs: list[Page] = []
        pages = self.session.open_pages() if self.session is not None else []
        contexts = {id(p.context): p.context for p in pages}
        if self.agent_page is not None and not self.agent_page.is_closed():
            contexts.setdefault(id(self.agent_page.context), self.agent_page.context)
        for context in contexts.values():
            tabs += [p for p in context.pages if not p.is_closed()]
        return tabs

    def _tab_ids(self, page: Page) -> set[int]:
        return {id(p) for p in page.context.pages}

    async def tab_list(self) -> list[str]:
        """For the snapshot: "[1] Flipkart search — flipkart.com (this one)"."""
        lines = []
        for n, tab in enumerate(self._all_tabs()[:MAX_TABS_SHOWN], 1):
            try:
                title = (await tab.title()) or "(no title)"
            except Exception:
                title = "(loading)"
            host = tab.url.split("/")[2] if "://" in tab.url else tab.url
            private = " (private)" if self._is_private(tab) else ""
            this = " (this one)" if tab is self.agent_page else ""
            lines.append(f"[{n}] {title[:60]} — {host}{private}{this}")
        return lines

    def _is_private(self, tab: Page) -> bool:
        pages = self.session.open_pages() if self.session is not None else []
        return bool(pages) and tab.context is not pages[0].context

    def _find_tab(self, text: str) -> Page | None:
        tabs = self._all_tabs()
        t = text.strip().strip("[]#").lower()
        if t.isdigit() and 1 <= int(t) <= len(tabs):
            return tabs[int(t) - 1]
        for tab in reversed(tabs):  # newest first: "the airdopes tab"
            if t and t in tab.url.lower():
                return tab
        return None

    async def _find_tab_by_title(self, text: str) -> Page | None:
        tab = self._find_tab(text)
        if tab is not None:
            return tab
        t = text.strip().lower()
        for candidate in reversed(self._all_tabs()):
            with contextlib.suppress(Exception):
                if t and t in (await candidate.title()).lower():
                    return candidate
        return None

    async def _use_tab(self, tab: Page) -> str:
        self.agent_page = tab
        await tab.bring_to_front()
        with contextlib.suppress(Exception):
            await tab.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
        await _settle(tab)
        try:
            return await tab.title() or tab.url
        except Exception:
            return tab.url

    async def _follow_new_tab(self, page: Page, before: set[int]) -> str:
        """The action opened a tab (a link meant for a new tab, a pop-up): like Chrome bringing a
        new foreground tab forward, work there — and say so."""
        new = [p for p in page.context.pages if id(p) not in before and not p.is_closed()]
        if not new:
            return ""
        tab = new[-1]
        self._opened.add(id(tab))
        title = await self._use_tab(tab)
        return f" — it opened a new tab: {title}"

    async def _tabs_op(
        self, page: Page, loc: Locator | None, action: Action, what: str
    ) -> ActionResult:
        kind, target = action.kind, action.target.strip()
        if kind == "tab_open":
            url = target
            if loc is not None:
                url = str(await loc.evaluate(_LINK_JS)) or ""
                if not url:  # a button, not a link: Ctrl+click, as a person would
                    before = self._tab_ids(page)
                    await loc.click(modifiers=["Control"], timeout=ELEMENT_TIMEOUT_MS)
                    await _settle(page)
                    note = await self._follow_new_tab(page, before)
                    return ok(f"Opened {what} in a new tab{note}") if note else fail(
                        "no_new_tab", f"{what} didn't open a new tab")  # fmt: skip
            if not url.startswith(("http://", "https://", "file://")):
                return fail("bad_url", "a new tab needs a link or a full http(s) address")
            tab = await page.context.new_page()
            self._opened.add(id(tab))
            await tab.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            return ok(f"Opened a new tab: {await self._use_tab(tab)}")
        if kind == "private_window":
            if not target.startswith(("http://", "https://", "file://")):
                return fail("bad_url", "a private window needs a full http(s) address")
            browser = page.context.browser
            if browser is None:
                return fail("no_private", "A private window isn't available here")
            context = await browser.new_context()  # no cookies, no sign-ins, nothing kept
            tab = await context.new_page()
            self._opened.add(id(tab))
            await tab.goto(target, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            title = await self._use_tab(tab)
            return ok(f"Opened a private window (not signed in, nothing is kept): {title}")
        if kind == "tab_switch":
            found = await self._find_tab_by_title(target)
            if found is None:
                return fail("no_tab", f'No open tab matches "{target}"')
            return ok(f"Switched to the tab: {await self._use_tab(found)}")
        # tab_close
        closing = await self._find_tab_by_title(target) if target else page
        if closing is None:
            return fail("no_tab", f'No open tab matches "{target}"')
        tab = closing
        tabs = self._all_tabs()
        if len(tabs) <= 1:
            return fail("last_tab", "That's the only tab — I'll leave it open")
        title = await tab.title()
        await tab.close()
        if tab is self.agent_page or self.agent_page is None or self.agent_page.is_closed():
            rest = [t for t in self._all_tabs() if not t.is_closed()]
            await self._use_tab(rest[-1])
        return ok(f"Closed the tab: {title}")

    async def _scroll(self, page: Page, where: str) -> ActionResult:
        """Scroll like a person: the mouse wheel over what scrolls here (a chat panel, a list, or
        the page) — and say honestly whether anything moved."""
        box = await page.evaluate(_SCROLLER_JS)
        if where in ("scroll_top", "scroll_bottom"):
            await page.evaluate(_SCROLL_END_JS, where == "scroll_top")
        else:
            step = max(100.0, 0.8 * float(box["height"]))
            await page.mouse.move(float(box["x"]), float(box["y"]))
            await page.mouse.wheel(0, -step if where == "scroll_up" else step)
        await page.wait_for_timeout(250)  # smooth scrolling takes a moment
        moved = abs(float(await page.evaluate(_SCROLL_POS_JS)) - float(box["top"])) > 2
        if not moved:
            end = "top" if where in ("scroll_up", "scroll_top") else "bottom"
            return ok(f"Nothing moved — already at the {end}")
        return ok(_SCROLLED[where])

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
            return await self._scroll(page, where)
        return fail("unsupported", f"Unknown navigation '{where}'")

    def _set_ad_policy(self, value: str) -> ActionResult:
        policy = _AD_POLICIES.get(value)
        if policy is None:
            return fail("unsupported", f"Unknown ad setting '{value}'")
        self.youtube.ad_policy = policy
        return ok({"skip": "I'll skip ads", "play": "I'll leave ads alone",
                   "ask": "I'll ask about ads"}[policy])  # fmt: skip
