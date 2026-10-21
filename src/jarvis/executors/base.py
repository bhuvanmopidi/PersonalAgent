"""The single path from an Action to execution: policy → (confirm/handoff) → executor."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from jarvis.models import Action, ActionResult
from jarvis.safety.confirm import Confirmer, build_confirm_prompt, build_handoff_message
from jarvis.safety.policy import Decision, SafetyPolicy
from jarvis.telemetry.logging import get_logger
from jarvis.telemetry.metrics import timed

log = get_logger(__name__)


class Executor(Protocol):
    async def execute(self, action: Action) -> ActionResult: ...


Annotator = Callable[[Action], Awaitable[Action]]


class ActionRunner:
    """Executors are never called directly — always through run_action()."""

    def __init__(
        self,
        policy: SafetyPolicy,
        executor: Executor,
        confirmer: Confirmer,
        annotate: Annotator | None = None,
        order_reader: Annotator | None = None,
    ) -> None:
        self.policy = policy
        self.executor = executor
        self.confirmer = confirmer
        self.annotate = annotate  # adds context the policy needs (e.g. the current page URL)
        # a pay / place-order step without its details: read them off the page (Phase 11.4)
        self.order_reader = order_reader

    async def run_action(self, action: Action) -> ActionResult:
        with timed(f"action:{action.kind}") as t:
            result = await self._run(action)
        return result.model_copy(update={"duration_ms": t.ms})

    async def _run(self, action: Action) -> ActionResult:
        if self.annotate is not None:
            action = await self.annotate(action)
        check = self.policy.check(action)
        if check.decision == Decision.REFUSE and check.missing and self.order_reader is not None:
            action = await self.order_reader(action)
            check = self.policy.check(action)  # the policy still decides — with what was read
        log.info(
            "policy kind=%s target=%r risk=%s decision=%s reasons=%s",
            action.kind,
            action.target,
            check.risk.name,
            check.decision,
            check.reasons,
        )

        if check.decision == Decision.REFUSE and check.missing:
            # a step that may place an order, without all five details on screen: never done —
            # but the agent may go where they are shown (the checkout page) and try there
            why = (
                f"Not done: this step may place an order or pay, and I couldn't read the "
                f"{', '.join(m.replace('_', ' ') for m in check.missing)} on this page"
            )
            if action.args.get("order_unverified"):
                why += f" (not shown word for word: {action.args['order_unverified']})"
            return ActionResult(ok=False, error="order_details_missing", observation=why + ". "
                                "Steps that may place an order are done only from a page that "
                                "shows the full order summary — reach it another way (e.g. the "
                                "cart, then checkout), or ask the user.")  # fmt: skip
        if check.decision == Decision.REFUSE:
            return ActionResult(ok=False, error="refused", observation="; ".join(check.reasons))

        if check.decision == Decision.HANDOFF:
            done = await self.confirmer.handoff(build_handoff_message(action, check))
            if not done:
                return ActionResult(ok=False, error="user_cancelled")
            return ActionResult(ok=True, observation="completed by user")

        confirmed = check.decision == Decision.CONFIRM
        question = build_confirm_prompt(action, check) if confirmed else ""
        if confirmed and not await self.confirmer.confirm(question):
            return ActionResult(ok=False, error="user_declined")

        try:
            result = await self.executor.execute(action)
        except Exception as exc:  # executor bugs become results, never crash the agent
            log.exception("executor failed for %s", action.kind)
            return ActionResult(ok=False, error=f"exception: {exc}")
        if confirmed:  # the agent must never repeat a step the user said yes to (a 2nd Send…)
            data = {**result.data, "confirmed": True, "asked": question}  # …and knows what was said
            result = result.model_copy(update={"data": data})
        return result
