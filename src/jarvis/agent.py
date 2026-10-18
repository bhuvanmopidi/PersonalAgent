"""Wires router → mapper → ActionRunner → executor. Text in, ActionResult out."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from jarvis.config import Settings
from jarvis.executors.base import ActionRunner
from jarvis.executors.os_executor import OSExecutor
from jarvis.models import Action, ActionResult, Intent
from jarvis.router.regex_router import RegexRouter
from jarvis.safety.confirm import Confirmer, TerminalConfirmer
from jarvis.safety.killswitch import Killed, KillSwitch
from jarvis.safety.policy import SafetyPolicy
from jarvis.telemetry.metrics import timed
from jarvis.tiers.instant.apps import AppLauncher, load_aliases
from jarvis.tiers.instant.files import FileTools
from jarvis.tiers.instant.mapper import to_action
from jarvis.tiers.instant.system import Brightness, Volume


@dataclass
class Outcome:
    text: str
    intent: Intent | None
    action: Action | None
    result: ActionResult
    route_ms: float


class Agent:
    def __init__(
        self,
        router: RegexRouter,
        runner: ActionRunner,
        kill: KillSwitch,
        launcher: AppLauncher | None = None,
    ) -> None:
        self.router, self.runner, self.kill, self.launcher = router, runner, kill, launcher

    @classmethod
    def create(cls, settings: Settings, confirmer: Confirmer | None = None) -> Agent:
        launcher = AppLauncher(load_aliases())
        executor = OSExecutor(launcher, Volume(), Brightness(), FileTools())
        runner = ActionRunner(SafetyPolicy.from_yaml(), executor, confirmer or TerminalConfirmer())
        return cls(RegexRouter.from_yaml(), runner, KillSwitch(settings.hotkeys.kill), launcher)

    async def prewarm(self) -> None:
        """Load the app index in the background so the first "open X" is instant."""
        if self.launcher:
            await asyncio.to_thread(self.launcher.prewarm)

    async def handle(self, text: str) -> Outcome:
        with timed("route") as t:
            intent = self.router.route(text)
        if intent is None:
            result = ActionResult(ok=False, error="no_match", observation="I can't do that yet.")
            return Outcome(text, None, None, result, t.ms)

        action = to_action(self.router.specs[intent.name], intent)
        try:
            result = await self.kill.guard(self.runner.run_action(action))
        except Killed as exc:
            result = ActionResult(ok=False, error="killed", observation=str(exc))
        return Outcome(text, intent, action, result, t.ms)
