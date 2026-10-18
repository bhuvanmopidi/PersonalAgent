"""Wires router → mapper → ActionRunner → executor, and speaks while acting."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from jarvis.config import Settings
from jarvis.executors.base import ActionRunner
from jarvis.executors.os_executor import OSExecutor
from jarvis.models import Action, ActionResult, Intent
from jarvis.router.regex_router import RegexRouter
from jarvis.safety.confirm import Confirmer, SpokenConfirmer, TerminalConfirmer
from jarvis.safety.killswitch import Killed, KillSwitch
from jarvis.safety.policy import SafetyPolicy
from jarvis.telemetry.metrics import timed
from jarvis.tiers.instant.apps import AppLauncher, load_aliases
from jarvis.tiers.instant.files import FileTools
from jarvis.tiers.instant.mapper import to_action
from jarvis.tiers.instant.system import Brightness, Volume
from jarvis.voice.audio import SoundDeviceSink
from jarvis.voice.speaker import Speaker
from jarvis.voice.tts import create_engine

# Failures the user caused or already knows about — no need to say them out loud.
_SILENT_ERRORS = {"user_declined", "user_cancelled", "killed"}


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
        speaker: Speaker | None = None,
        launcher: AppLauncher | None = None,
    ) -> None:
        self.router, self.runner, self.kill = router, runner, kill
        self.speaker = speaker or Speaker(None, SoundDeviceSink())  # silent by default
        self.launcher = launcher

    @classmethod
    def create(
        cls, settings: Settings, confirmer: Confirmer | None = None, mute: bool = False
    ) -> Agent:
        tts = settings.tts
        engine = None if mute else create_engine(tts.engine, tts.voice, tts.speed)
        speaker = Speaker(engine, SoundDeviceSink())
        launcher = AppLauncher(load_aliases())
        executor = OSExecutor(launcher, Volume(), Brightness(), FileTools())
        spoken = SpokenConfirmer(confirmer or TerminalConfirmer(), speaker)
        runner = ActionRunner(SafetyPolicy.from_yaml(), executor, spoken)
        kill = KillSwitch(settings.hotkeys.kill)
        return cls(RegexRouter.from_yaml(), runner, kill, speaker, launcher)

    async def prewarm(self) -> None:
        """Load the app index and the voice model in the background so first use is instant."""
        jobs = [self.speaker.prewarm()]
        if self.launcher:
            jobs.append(asyncio.to_thread(self.launcher.prewarm))
        await asyncio.gather(*jobs)

    async def handle(self, text: str) -> Outcome:
        with timed("route") as t:
            intent = self.router.route(text)
        if intent is None:
            result = ActionResult(ok=False, error="no_match", observation="I can't do that yet.")
            await self.speaker.say(result.observation)
            return Outcome(text, None, None, result, t.ms)

        spec = self.router.specs[intent.name]
        action = to_action(spec, intent)
        if spec.speak == "description":
            await self.speaker.say(action.description)  # speaks while the action runs

        try:
            result = await self.kill.guard(self.runner.run_action(action))
        except Killed as exc:
            self.speaker.stop()
            result = ActionResult(ok=False, error="killed", observation=str(exc))

        failed_loudly = not result.ok and result.error not in _SILENT_ERRORS
        if failed_loudly or (result.ok and spec.speak == "result"):
            await self.speaker.say(result.observation)
        return Outcome(text, intent, action, result, t.ms)
