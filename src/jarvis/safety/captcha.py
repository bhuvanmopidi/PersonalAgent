"""CAPTCHA / "verify you are human" handling: detect, hand to the user, resume. Never solved.

Detection uses facts on the page: iframes from known challenge providers (reCAPTCHA, hCaptcha,
Cloudflare Turnstile, Arkose) and challenge phrases in the title / top of the page.

The handoff goes through ActionRunner like any action (kind `solve_captcha` + flag
`captcha_detected` → RED → handoff), so it is spoken and logged the usual way.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from jarvis.models import Action, ActionResult
from jarvis.safety.confirm import can_cancel
from jarvis.safety.policy import CaptchaRules
from jarvis.telemetry.logging import get_logger

if TYPE_CHECKING:
    from playwright.async_api import Page

log = get_logger(__name__)

POLL_S = 0.5

_DETECT_JS = """(rules) => {
  // only a frame you can SEE is a challenge: many sites preload invisible reCAPTCHA helpers
  // (0×0, display:none) on every page, and those need nothing from the user
  const seen = f => {
    const r = f.getBoundingClientRect(), s = getComputedStyle(f);
    return r.width >= 20 && r.height >= 20 && s.display !== 'none' &&
      s.visibility !== 'hidden' && s.opacity !== '0';
  };
  const frames = [...document.querySelectorAll('iframe')].filter(seen);
  const frame = frames.find(f => rules.iframe_hosts.some(h => (f.src || '').includes(h)));
  if (frame) return 'iframe ' + frame.src.split('?')[0];
  const body = document.body ? document.body.innerText.slice(0, 3000) : '';
  const text = (document.title + ' ' + body).toLowerCase();
  const phrase = rules.phrases.find(p => text.includes(p));
  return phrase ? 'text "' + phrase + '"' : null;
}"""

Escalate = Callable[[Action], Coroutine[Any, Any, ActionResult]]


async def detect(page: Page, rules: CaptchaRules) -> str | None:
    """What gave the challenge away, or None. Errors (page navigating) count as nothing found."""
    try:
        found = await page.evaluate(_DETECT_JS, rules.model_dump())
    except Exception:
        return None
    return str(found) if found else None


async def wait_until_gone(page: Page, rules: CaptchaRules) -> None:
    while await detect(page, rules):
        await asyncio.sleep(POLL_S)


class CaptchaGate:
    def __init__(self, rules: CaptchaRules, confirmer: object) -> None:
        self.rules = rules
        self.confirmer = confirmer  # only asked whether its questions can be cancelled
        self.escalate: Escalate | None = None  # ActionRunner.run_action, wired by the agent

    async def check(self, page: Page) -> ActionResult | None:
        """None = no challenge (or it was cleared). A result = stop the task with it."""
        found = await detect(page, self.rules)
        if not found:
            return None
        log.info("captcha detected on %s (%s)", page.url, found)
        if self.escalate is None:  # fail closed
            return ActionResult(ok=False, error="captcha", observation="A human check needs you.")

        await page.bring_to_front()
        action = Action(
            kind="solve_captcha",
            target=page.url,
            description="complete the human check in the Chrome window",
            flags={"captcha_detected"},
            page_url=page.url,
        )
        handoff = asyncio.create_task(self.escalate(action))
        if can_cancel(self.confirmer):
            # resume as soon as the challenge disappears, even if the user never says "done"
            gone = asyncio.create_task(wait_until_gone(page, self.rules))
            done, _ = await asyncio.wait({handoff, gone}, return_when=asyncio.FIRST_COMPLETED)
            for task in (handoff, gone):
                if task not in done:
                    task.cancel()
            await asyncio.gather(handoff, gone, return_exceptions=True)
            if gone in done:
                log.info("captcha cleared — resuming")
                return None
        result = await handoff
        if not result.ok:
            return ActionResult(
                ok=False, error="user_cancelled", observation="Stopped at the check."
            )
        if await detect(page, self.rules):
            return ActionResult(
                ok=False, error="captcha", observation="The human check is still on the page."
            )
        return None
