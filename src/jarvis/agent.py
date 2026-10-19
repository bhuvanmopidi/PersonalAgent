"""Wires router → mapper → ActionRunner → executor, and speaks while acting.

Understanding a command (Phase 7):
  hybrid (default): regex rules → if they miss, the LLM (Claude or the local model)
  llm_first:        the LLM → if it can't answer, the rules (an experiment mode)
The LLM only chooses among the same intents, so every action still goes through ActionRunner.
"""

from __future__ import annotations

import asyncio
import importlib
import time
from dataclasses import dataclass
from typing import Literal

from jarvis.apps.youtube_flow import load_youtube_config
from jarvis.config import Settings
from jarvis.executors.base import ActionRunner
from jarvis.executors.browser import BROWSER_KINDS, BrowserExecutor
from jarvis.executors.browser_session import BrowserSession, BrowserUnavailable
from jarvis.executors.desktop import DesktopExecutor, DesktopSession
from jarvis.executors.llm_control import LLM_KINDS, LLMControlExecutor
from jarvis.executors.os_executor import OSExecutor
from jarvis.executors.router import KindRouter
from jarvis.models import Action, ActionResult, Intent
from jarvis.planner.catalog import Catalog
from jarvis.planner.connectivity import Connectivity
from jarvis.planner.llm import (
    ClaudeProvider,
    LLMProvider,
    OllamaProvider,
    OpenAIProvider,
    OpenRouterProvider,
)
from jarvis.planner.router import LLMRouter, Routed
from jarvis.router.context import SessionContext
from jarvis.router.log import RouteLog
from jarvis.router.regex_router import RegexRouter
from jarvis.safety.confirm import Confirmer, SpokenConfirmer, TerminalConfirmer
from jarvis.safety.killswitch import Killed, KillSwitch
from jarvis.safety.policy import SafetyPolicy
from jarvis.telemetry.logging import get_logger
from jarvis.tiers.instant.apps import AppLauncher, load_aliases, parse_prefer
from jarvis.tiers.instant.files import FileTools
from jarvis.tiers.instant.mapper import to_action
from jarvis.tiers.instant.system import Brightness, Volume
from jarvis.tiers.structured.agent import TaskAgent, default_trace_dir
from jarvis.tiers.structured.surface import BrowserSurface, DesktopSurface, Surface, SurfaceName
from jarvis.voice.audio import SoundDeviceSink
from jarvis.voice.speaker import Speaker
from jarvis.voice.tts import create_engine

log = get_logger(__name__)

# Failures the user caused or already knows about — no need to say them out loud.
_SILENT_ERRORS = {"user_declined", "user_cancelled", "killed"}
ONE_MOMENT_AFTER_S = 0.6  # LLM slower than this → say "One moment" so there's no silence

Mode = Literal["hybrid", "llm_first"]


@dataclass
class Outcome:
    text: str
    intent: Intent | None
    action: Action | None
    result: ActionResult
    route_ms: float
    route: str = "rules"  # rules | claude | local | none
    reply: str | None = None  # answer / clarify / unsupported: what was said instead of acting


class Agent:
    def __init__(
        self,
        router: RegexRouter,
        runner: ActionRunner,
        kill: KillSwitch,
        speaker: Speaker | None = None,
        launcher: AppLauncher | None = None,
        browser: BrowserSession | None = None,
        llm: LLMRouter | None = None,
        mode: Mode = "hybrid",
        route_log: RouteLog | None = None,
        task_agent: TaskAgent | None = None,
    ) -> None:
        self.router, self.runner, self.kill = router, runner, kill
        self.speaker = speaker or Speaker(None, SoundDeviceSink())  # silent by default
        self.launcher = launcher
        self.browser = browser
        self.llm = llm
        self.mode: Mode = mode
        self.context = SessionContext()
        self.route_log = route_log
        self.task_agent = task_agent  # the general agent: websites (Phase 11), apps (11.2)

    @classmethod
    def create(
        cls, settings: Settings, confirmer: Confirmer | None = None, mute: bool = False
    ) -> Agent:
        tts = settings.tts
        engine = None if mute else create_engine(tts.engine, tts.voice, tts.speed)
        speaker = Speaker(engine, SoundDeviceSink())
        spoken = SpokenConfirmer(confirmer or TerminalConfirmer(), speaker)
        policy = SafetyPolicy.from_yaml()
        rules = RegexRouter.from_yaml()

        session = BrowserSession(settings.browser) if settings.browser.enabled else None
        browser = BrowserExecutor(
            session, load_youtube_config(), policy.rules.captcha, spoken, speaker.say
        )

        async def open_site(url: str) -> ActionResult:  # "open instagram" → the Jarvis Chrome
            return await browser.execute(Action(kind="open_url", target=url))

        llm = build_llm_router(settings, rules) if settings.llm.enabled else None
        launcher = AppLauncher(load_aliases())
        system = OSExecutor(launcher, Volume(), Brightness(), FileTools(), url_opener=open_site)
        desktop = DesktopSession()
        executor = KindRouter(
            system, [(BROWSER_KINDS, browser), (LLM_KINDS, LLMControlExecutor(llm))],
            surfaces={"desktop": DesktopExecutor(desktop, launcher)},
        )  # fmt: skip
        runner = ActionRunner(policy, executor, spoken, annotate=browser.annotate)
        browser.captcha.escalate = runner.run_action  # CAPTCHA handoffs go through the runner too
        kill = KillSwitch(settings.hotkeys.kill)
        task_agent = None
        if llm is not None:
            surfaces: dict[SurfaceName, Surface] = {"desktop": DesktopSurface(desktop)}
            if session is not None:
                surfaces["browser"] = BrowserSurface(browser)
            task_agent = TaskAgent(
                build_agent_planners(settings, llm.connectivity), surfaces, runner.run_action,
                settings.agent, trace_dir=default_trace_dir(),
            )  # fmt: skip
        return cls(
            rules, runner, kill, speaker, launcher, session,
            llm=llm, mode=settings.router.mode, route_log=RouteLog(), task_agent=task_agent,
        )  # fmt: skip

    async def prewarm(self, launch_browser: bool = False) -> None:
        """Load the app index, the voice, the browser (and the local model if it's in use) in the
        background so first use is instant. With launch_browser the Jarvis Chrome is started too."""
        jobs = [self.speaker.prewarm()]
        if self.launcher:
            jobs.append(asyncio.to_thread(self.launcher.prewarm))
        if self.browser:
            jobs.append(self._connect_browser(launch_browser))
        if self.llm:
            jobs.append(self._warm_llm())
        await asyncio.gather(*jobs)

    async def _warm_llm(self) -> None:
        assert self.llm is not None
        # cloud SDKs take ~1–2 s to import: do it now, not in the middle of a command
        sdk = {"openrouter": "openai", "openai": "openai", "claude": "anthropic"}.get(
            self.llm.online_provider
        )
        if sdk:
            await asyncio.to_thread(importlib.import_module, sdk)
        cloud = self.llm.providers.get(self.llm.online_provider)
        if isinstance(cloud, OpenAIProvider) and await self.llm.connectivity.online():
            cloud.warm_system = self.llm.system
            await cloud.warm()  # connection + prompt cache ready before the first command
        local = self.llm.providers.get("local")
        uses_local = self.llm.online_provider == "local" or not await self.llm.connectivity.online()
        if isinstance(local, OllamaProvider) and uses_local:
            await local.warm()

    async def _connect_browser(self, launch: bool) -> None:
        assert self.browser is not None
        try:
            await self.browser.connect(launch=launch)
        except BrowserUnavailable as exc:
            log.info("browser not connected at startup: %s", exc)
        except Exception:  # never let the browser block startup
            log.warning("browser prewarm failed", exc_info=True)

    async def close(self) -> None:
        self.speaker.stop()
        if self.browser:
            await self.browser.close()

    # ---- understanding ---------------------------------------------------------------------

    async def understand(self, text: str) -> tuple[Intent | None, Routed | None, str]:
        """(intent, what the LLM said if it was asked, route)."""
        if self.mode == "hybrid":
            intent = self.router.route(text)
            if intent is not None and not await self._rules_cannot_resolve(intent):
                return intent, None, "rules"
        routed = await self._ask_llm(text) if self.llm else None
        if routed is not None and (routed.intent or routed.decision):
            return routed.intent, routed, routed.provider or "llm"
        if self.mode == "llm_first":  # the LLM couldn't answer → rules as the backup
            intent = self.router.route(text)
            if intent is not None:
                return intent, routed, "rules"
        return None, routed, "none"

    async def _rules_cannot_resolve(self, intent: Intent) -> bool:
        """'open <something>' where <something> is no app or site we know: the catch-all rule only
        guessed — let the LLM understand the sentence instead (only when an LLM is there to ask)."""
        if intent.name != "open_app" or self.llm is None or self.launcher is None:
            return False
        prefer = parse_prefer(str(intent.args.get("prefer", "")))
        plan = await asyncio.to_thread(
            self.launcher.resolve, str(intent.args.get("app", "")), prefer
        )
        return plan is None

    async def _ask_llm(self, text: str) -> Routed:
        assert self.llm is not None
        task = asyncio.create_task(self.llm.route(text, self.context.render()))
        done, _ = await asyncio.wait({task}, timeout=ONE_MOMENT_AFTER_S)
        if not done:
            await self.speaker.say("One moment.")
        return await task

    # ---- handling --------------------------------------------------------------------------

    async def handle(self, text: str) -> Outcome:
        start = time.perf_counter()
        intent, routed, route = await self.understand(text)
        route_ms = (time.perf_counter() - start) * 1000

        decision = routed.decision if routed else None
        if intent is None and decision is not None and decision.kind == "task":
            where: SurfaceName = decision.where or "browser"
            outcome = await self._run_task(text, decision.goal or text, route, route_ms, where)
        elif intent is None:
            outcome = await self._reply_without_acting(text, routed, route, route_ms)
        else:
            outcome = await self._act(text, intent, route, route_ms)
        self._remember(outcome, routed)
        return outcome

    async def _run_task(
        self, text: str, goal: str, route: str, route_ms: float, where: SurfaceName = "browser"
    ) -> Outcome:
        """No single command does it: the general agent works it out on the page or in the app."""
        task = Intent(name="task", args={"goal": goal, "where": where}, source="llm")
        if self.task_agent is None or where not in self.task_agent.surfaces:
            what = "app" if where == "desktop" else "browser"
            reply = f"I can't do that yet — the {what} agent is switched off."
            result = ActionResult(ok=False, error="no_agent", observation=reply)
        elif self.llm is not None and not await self.llm.connectivity.online():
            reply = "I need the internet for that."
            result = ActionResult(ok=False, error="offline", observation=reply)
        else:
            await self.speaker.say("Working on it.")
            try:
                done = await self.kill.guard(self.task_agent.run(goal, where))
                reply = done.message
                result = ActionResult(ok=done.ok, error=done.error, observation=reply,
                                      duration_ms=done.ms)  # fmt: skip
            except Killed as exc:
                self.speaker.stop()
                reply = str(exc)
                result = ActionResult(ok=False, error="killed", observation=reply)
        if result.error not in _SILENT_ERRORS:
            await self.speaker.say(reply)
        return Outcome(text, task, None, result, route_ms, route=route, reply=reply)

    async def _reply_without_acting(
        self, text: str, routed: Routed | None, route: str, route_ms: float
    ) -> Outcome:
        decision = routed.decision if routed else None
        if decision is not None and decision.kind in ("answer", "clarify"):
            reply = (decision.text or "").strip()
            result = ActionResult(ok=True, observation=reply)
        elif decision is not None and decision.kind == "unsupported":
            reply = f"I can't do that yet. {(decision.text or '').strip()}".strip()
            result = ActionResult(ok=False, error="unsupported", observation=reply)
        else:
            reply = "I can't do that yet."
            if routed is not None and routed.error:
                reply = f"I didn't understand that, and {routed.error}."
            result = ActionResult(ok=False, error="no_match", observation=reply)
        await self.speaker.say(reply)
        return Outcome(text, None, None, result, route_ms, route=route, reply=reply)

    async def _act(self, text: str, intent: Intent, route: str, route_ms: float) -> Outcome:
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
        return Outcome(text, intent, action, result, route_ms, route=route)

    def _remember(self, outcome: Outcome, routed: Routed | None) -> None:
        if outcome.intent is not None:
            args = ", ".join(f"{k}={v}" for k, v in outcome.intent.args.items() if v)
            did = f"{outcome.intent.name}({args}) -> {outcome.result.observation}"
        else:
            did = f"replied: {outcome.reply}"
        self.context.add(outcome.text, did)
        if self.route_log is None:
            return
        usage = routed.usage if routed and routed.provider else None
        self.route_log.write(
            outcome.text,
            mode=self.mode,
            route=outcome.route,
            intent=outcome.intent.name if outcome.intent else None,
            args=outcome.intent.args if outcome.intent else None,
            kind=routed.decision.kind if routed and routed.decision else None,
            ok=outcome.result.ok,
            route_ms=round(outcome.route_ms, 1),
            model=routed.model if routed else None,
            attempts=routed.attempts if routed else None,
            tokens=vars(usage) if usage else None,
        )


def build_agent_planners(settings: Settings, connectivity: Connectivity) -> list[LLMProvider]:
    """Who plans the general agent's steps: the agent model via OpenRouter first, then the other
    cloud providers if they have keys. The local 3B model is too weak for multi-step pages."""
    s = settings.llm
    return [
        OpenRouterProvider(s, on_offline=connectivity.mark_offline, model=s.agent_model,
                           timeout_s=s.agent_timeout_s),
        OpenAIProvider(s, on_offline=connectivity.mark_offline, timeout_s=s.agent_timeout_s),
        ClaudeProvider(s, on_offline=connectivity.mark_offline),
    ]  # fmt: skip


def build_llm_router(settings: Settings, rules: RegexRouter) -> LLMRouter:
    connectivity = Connectivity()
    providers: dict[str, LLMProvider] = {
        "openrouter": OpenRouterProvider(settings.llm, on_offline=connectivity.mark_offline),
        "openai": OpenAIProvider(settings.llm, on_offline=connectivity.mark_offline),
        "claude": ClaudeProvider(settings.llm, on_offline=connectivity.mark_offline),
        "local": OllamaProvider(settings.llm),
    }
    catalog = Catalog(rules.specs.values())
    return LLMRouter(catalog, providers, connectivity, settings.llm.online_provider)
