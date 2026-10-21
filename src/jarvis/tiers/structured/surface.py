"""Where the general agent works: a browser tab or a Windows app window. The See→Think→Act→Check
loop (agent.py) is the same for both; a Surface is how it looks and how it notices change.

  browser — the Jarvis Chrome tab, read from the DOM (observer.py), CAPTCHAs handed over
  desktop — an app window, read through UI Automation (desktop_observer.py), administrator
            prompts never touched
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Literal, Protocol

from jarvis.models import ActionResult
from jarvis.tiers.structured.observer import Snapshot, dom_size, observe, settle, still_there

if TYPE_CHECKING:
    from playwright.async_api import Page

    from jarvis.executors.browser import BrowserExecutor
    from jarvis.executors.desktop import DesktopSession

SurfaceName = Literal["browser", "desktop"]


class Shot:
    """A screenshot and how to turn its pixels back into a click position."""

    def __init__(self, image: Any, origin: tuple[float, float] = (0, 0), scale: float = 1.0):
        self.image = image  # PIL image
        self.origin = origin  # where the picture's top-left is (desktop: on the screen)
        self.scale = scale  # picture pixels per click unit

    def to_click(self, x: float, y: float) -> tuple[float, float]:
        return self.origin[0] + x / self.scale, self.origin[1] + y / self.scale


class Surface(Protocol):
    name: SurfaceName

    async def blocked(self) -> ActionResult | None:
        """Something only the user may handle (a CAPTCHA, an admin prompt): stop with it."""
        ...

    async def observe(self, max_elements: int, max_text: int) -> Snapshot: ...
    async def still_there(self, element_id: int) -> bool: ...
    async def size(self) -> int: ...
    async def settle(self) -> None: ...
    def location(self) -> str:
        """Changes when the agent lands somewhere else (a new page / another window)."""
        ...

    async def screenshot(self) -> Shot | None:
        """What's on screen where the agent works (kept in memory only), or None."""
        ...


class BrowserSurface:
    name: SurfaceName = "browser"

    def __init__(self, browser: BrowserExecutor) -> None:
        self.browser = browser
        self._url = ""

    async def _page(self) -> Page:
        return await self.browser.agent_tab()

    async def blocked(self) -> ActionResult | None:
        page = await self._page()
        return await self.browser.captcha.check(page)

    async def observe(self, max_elements: int, max_text: int) -> Snapshot:
        page = await self._page()
        snap = await observe(page, max_elements, max_text)
        self._url = page.url
        # the tab strip too: a tab that opened, or the wrong tab, is visible to the planner
        return snap.model_copy(update={"tabs": await self.browser.tab_list()})

    async def still_there(self, element_id: int) -> bool:
        return await still_there(await self._page(), element_id)

    async def size(self) -> int:
        page = await self._page()
        self._url = page.url
        return await dom_size(page)

    async def settle(self) -> None:
        await settle(await self._page())

    def location(self) -> str:
        page = self.browser.agent_page
        return page.url if page is not None and not page.is_closed() else self._url

    async def screenshot(self) -> Shot | None:
        import io

        from PIL import Image

        page = await self._page()
        # the visible part of the tab, in page (CSS) pixels — clicks go to the same coordinates
        png = await page.screenshot(type="png", scale="css")
        return Shot(Image.open(io.BytesIO(png)))


class DesktopSurface:
    name: SurfaceName = "desktop"

    def __init__(self, session: DesktopSession) -> None:
        self.session = session

    async def blocked(self) -> ActionResult | None:
        if await self.session.needs_admin():
            return ActionResult(
                ok=False,
                error="admin",
                observation="Windows is asking for administrator permission. I never use admin "
                "rights — approve it yourself if you want it, then ask me again.",
            )
        return None

    async def observe(self, max_elements: int, max_text: int) -> Snapshot:
        await self.session.settle()  # like a page: look once the window has stopped changing
        return await self.session.observe(max_elements, max_text)

    async def still_there(self, element_id: int) -> bool:
        return await self.session.still_there(element_id)

    async def size(self) -> int:
        return await self.session.size()

    async def settle(self) -> None:
        await self.session.settle()

    def location(self) -> str:
        window = self.session.window
        return f"{self.session.location()}#{window.handle}" if window else "app://none"

    async def screenshot(self) -> Shot | None:
        rect = await self.session.window_rect()
        if rect is None:
            return None
        left, top, right, bottom = rect

        def grab() -> Any:
            import mss
            from PIL import Image

            with mss.mss() as screen:
                raw = screen.grab({"left": left, "top": top, "width": right - left,
                                   "height": bottom - top})  # fmt: skip
                return Image.frombytes("RGB", raw.size, raw.rgb)

        image = await asyncio.to_thread(grab)
        return Shot(image, origin=(left, top))
