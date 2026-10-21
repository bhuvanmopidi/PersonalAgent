"""The Jarvis Chrome window: a real, visible Chrome using its own data folder, driven over CDP.

Chrome (136+) refuses remote control of its default data folder (where your normal profiles and
logins live), so Jarvis runs Chrome with a separate `--user-data-dir`. You sign in there once
(`jarvis browser setup`); it is never headless, and your normal Chrome is never touched.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import psutil

from jarvis.config import BrowserSettings
from jarvis.paths import data_dir
from jarvis.telemetry.logging import get_logger

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page, Playwright

log = get_logger(__name__)

_CHROME_CANDIDATES = [
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
    / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
    / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
]
# detached: Chrome keeps running when Jarvis exits (or you press Ctrl+C in its terminal)
_DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
)


class BrowserUnavailable(Exception):
    """Chrome can't be found, started or connected to."""


class PageSource(Protocol):
    """What the browser executor needs: tabs. BrowserSession (real) and test sessions provide it."""

    async def pages(self) -> list[Page]: ...  # connects (and launches) if needed
    async def new_page(self) -> Page: ...
    def open_pages(self) -> list[Page]: ...  # only if already connected — never launches
    async def reset(self) -> None: ...  # forget a connection that went stale (window closed)


def find_chrome(configured: str = "") -> Path | None:
    candidates = [Path(configured)] if configured else _CHROME_CANDIDATES
    return next((p for p in candidates if p.is_file()), None)


def profile_dir(settings: BrowserSettings) -> Path:
    path = Path(settings.profile_dir) if settings.profile_dir else data_dir() / "chrome-profile"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cdp_ready(port: int, timeout: float = 0.5) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout):
            return True
    except OSError:
        return False


def jarvis_chrome_processes(profile: Path) -> list[psutil.Process]:
    """Chrome processes using the Jarvis data folder (whatever flags they were started with)."""
    needle = str(profile).lower()
    found = []
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if (proc.info["name"] or "").lower() != "chrome.exe":
                continue
            cmdline = " ".join(proc.info["cmdline"] or []).lower()
        except (psutil.Error, OSError):
            continue
        if needle in cmdline and "--type=" not in cmdline:  # the main process, not renderers
            found.append(proc)
    return found


def chrome_args(
    chrome: Path, profile: Path, port: int | None, urls: Sequence[str] = ()
) -> list[str]:
    args = [
        str(chrome),
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if port is not None:
        args.append(f"--remote-debugging-port={port}")  # listens on 127.0.0.1 only
    return [*args, *urls]


async def pick_active(pages: Sequence[Page]) -> Page | None:
    """The tab the user is looking at: visible + focused, else visible, else the newest."""
    visible = None
    for page in reversed(pages):
        try:
            state, focused = await page.evaluate("[document.visibilityState, document.hasFocus()]")
        except Exception:  # chrome:// pages, closing tabs
            continue
        if state == "visible" and focused:
            return page
        if state == "visible" and visible is None:
            visible = page
    return visible or (pages[-1] if pages else None)


class BrowserSession:
    def __init__(
        self,
        settings: BrowserSettings,
        popen: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        self.settings = settings
        self._popen = popen
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return (
            self._browser is not None
            and self._browser.is_connected()
            and bool(self._browser.contexts)
        )

    async def connect(self, launch: bool = True) -> BrowserContext:
        async with self._lock:
            if not self.connected:
                port = self.settings.cdp_port
                if not await asyncio.to_thread(cdp_ready, port):
                    if not launch:
                        raise BrowserUnavailable("Jarvis Chrome isn't running")
                    await self._launch()
                if self._pw is None:
                    from playwright.async_api import async_playwright

                    self._pw = await async_playwright().start()
                start = time.perf_counter()
                self._browser = await self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                log.info(
                    "connected to Jarvis Chrome in %.0f ms", (time.perf_counter() - start) * 1000
                )
            assert self._browser is not None
            contexts = self._browser.contexts
            if not contexts:
                raise BrowserUnavailable("Chrome has no window open")
            return contexts[0]

    async def pages(self) -> list[Page]:
        context = await self.connect()
        return [p for p in context.pages if not p.is_closed()] or [await context.new_page()]

    async def new_page(self) -> Page:
        return await (await self.connect()).new_page()

    def open_pages(self) -> list[Page]:
        if not self.connected:
            return []
        assert self._browser is not None
        contexts = self._browser.contexts
        return [p for p in contexts[0].pages if not p.is_closed()] if contexts else []

    async def reset(self) -> None:
        """Forget the current connection; the next use reconnects, relaunching Chrome if needed."""
        async with self._lock:
            browser, self._browser = self._browser, None
        if browser is not None:
            try:
                await browser.close()  # for a CDP connection this only disconnects
            except Exception:
                log.debug("stale browser disconnect failed", exc_info=True)

    async def close(self) -> None:
        """Disconnect (Chrome itself stays open)."""
        self._browser = None
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None

    async def _launch(self) -> None:
        chrome = find_chrome(self.settings.chrome_path)
        if chrome is None:
            raise BrowserUnavailable("Google Chrome isn't installed (or set browser.chrome_path)")
        profile = profile_dir(self.settings)
        if await asyncio.to_thread(jarvis_chrome_processes, profile):
            # e.g. still open from `jarvis browser setup`, which runs without remote control
            raise BrowserUnavailable(
                "the window from `jarvis browser setup` is still open — close it (your sign-ins "
                "are kept), then ask again"
            )
        port = self.settings.cdp_port
        log.info("launching Jarvis Chrome (port %d, profile %s)", port, profile)
        self._popen(chrome_args(chrome, profile, port), creationflags=_DETACHED, close_fds=True)
        deadline = time.monotonic() + self.settings.launch_timeout_s
        while time.monotonic() < deadline:
            if await asyncio.to_thread(cdp_ready, port):
                return
            await asyncio.sleep(0.2)
        raise BrowserUnavailable(f"Chrome didn't open its control port {port} in time")
