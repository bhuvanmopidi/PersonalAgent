"""`jarvis demo-safety`: run fake actions through the real safety layer to see it work."""

from __future__ import annotations

import asyncio

from jarvis.config import Settings
from jarvis.executors.base import ActionRunner
from jarvis.models import Action, ActionResult
from jarvis.safety.confirm import TerminalConfirmer
from jarvis.safety.killswitch import Killed, KillSwitch
from jarvis.safety.policy import SafetyPolicy
from jarvis.telemetry.logging import get_logger

log = get_logger(__name__)


class FakeExecutor:
    """Pretends to act; sleeps `args.seconds` so there is time to press the kill key."""

    async def execute(self, action: Action) -> ActionResult:
        await asyncio.sleep(float(action.args.get("seconds", 0.2)))
        return ActionResult(ok=True, observation=f"(pretend) did {action.kind} {action.target}")


DEMO_ACTIONS = [
    Action(kind="launch_app", target="notepad", description="Open Notepad"),
    Action(
        kind="send",
        target="Gmail",
        description="Send email to arjun@gmail.com with Resume.pdf",
        args={"subject": "My resume"},
    ),
    Action(
        kind="pay",
        target="Pay now",
        description="Pay for order",
        args={
            "item": "boAt Airdopes 141",
            "quantity": 1,
            "price": "Rs 1,299",
            "address": "Home",
            "payment_method": "saved HDFC card",
        },
    ),
    Action(
        kind="pay",
        target="Pay now",
        description="Pay with missing details",
        args={"price": "Rs 99"},
    ),
    Action(kind="type_text", target="OTP", description="Enter the OTP", flags={"otp_field"}),
    Action(
        kind="wait",
        target="long task",
        description="Long task: press the kill key now",
        args={"seconds": 5},
    ),
]


async def run_demo(settings: Settings) -> None:
    runner = ActionRunner(SafetyPolicy.from_yaml(), FakeExecutor(), TerminalConfirmer())
    kill = KillSwitch(settings.hotkeys.kill)

    log.info("Redaction check: my OTP is 482913 and card 4111 1111 1111 1111")

    for action in DEMO_ACTIONS:
        print(f"\n>> {action.description}")
        try:
            result = await kill.guard(runner.run_action(action))
        except Killed as exc:
            result = ActionResult(ok=False, error="killed", observation=str(exc))
        print(
            f"   result: ok={result.ok} error={result.error} "
            f"{result.observation} ({result.duration_ms:.0f} ms)"
        )
