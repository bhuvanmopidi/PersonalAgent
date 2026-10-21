"""General-agent benchmark: the real planner model on local test sites (headless) and in Windows
apps (the predictable test app evals/apps/testapp.ps1, and built-in apps like Calculator).

  uv run jarvis eval-agent [--tasks id1,id2]

Each task starts on a page from evals/sites/agent (or in an app, `where: desktop`), gives the agent
a goal, answers confirmations as scripted, and checks the end state (URL, page / window text, the
spoken message, the outcome). Reports success, steps, actions, time and model cost per task.
Desktop tasks use the real screen, mouse and keyboard for a few seconds each.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from jarvis.apps.youtube_flow import load_youtube_config
from jarvis.config import PROJECT_ROOT, Settings
from jarvis.executors.base import ActionRunner
from jarvis.executors.browser import BrowserExecutor
from jarvis.executors.desktop import DesktopExecutor, DesktopSession
from jarvis.executors.files import FileResolver, chain
from jarvis.planner.connectivity import Connectivity
from jarvis.planner.evaluate import cost, openrouter_prices
from jarvis.safety.checkout import OrderReader
from jarvis.safety.confirm import ScriptedConfirmer
from jarvis.safety.policy import SafetyPolicy
from jarvis.tiers.instant.apps import AppLauncher, load_aliases
from jarvis.tiers.structured.agent import AgentOutcome, BrowserAgent, TaskAgent, default_trace_dir
from jarvis.tiers.structured.surface import DesktopSurface
from jarvis.tiers.vision.eyes import Eyes

TASKS_PATH = PROJECT_ROOT / "evals" / "agent_tasks.yaml"
SITES = PROJECT_ROOT / "evals" / "sites" / "agent"
TEST_APP = PROJECT_ROOT / "evals" / "apps" / "testapp.ps1"
TEST_APP_TITLE = "Jarvis Test App"
TEST_FILES = PROJECT_ROOT / "evals" / "files"
EYES = Eyes()  # one detector for the whole run (loaded once, on first use)


def task_folders(task: AgentTask) -> tuple[Path, Path]:
    """A fresh temp "user folder" holding only the task's files, and a downloads folder — so evals
    never search (or download into) the user's real folders."""
    import shutil
    import tempfile

    base = Path(tempfile.mkdtemp(prefix=f"jarvis-eval-{task.id}-"))
    files, downloads = base / "Documents", base / "Downloads"
    files.mkdir()
    downloads.mkdir()
    for name in task.files:
        shutil.copy(TEST_FILES / name, files / name)
    return files, downloads


RESULTS_DIR = PROJECT_ROOT / "evals" / "results"


class AgentTask(BaseModel):
    id: str
    goal: str
    start: str  # browser: a page in evals/sites/agent · desktop: "testapp", or "" (agent opens it)
    where: Literal["browser", "desktop"] = "browser"
    close_after: str = ""  # desktop: an app to close afterwards (e.g. "calculator")
    files: list[str] = Field(default_factory=list)  # from evals/files: the task's "user folders"
    answers: list[bool] = Field(default_factory=list)
    success: dict[str, Any] = Field(default_factory=dict)


def launch_test_app(title: str = TEST_APP_TITLE) -> subprocess.Popen[bytes]:
    """The predictable Windows Forms test app (text boxes, a drop-down, a password box, a long
    list, a modal dialog)."""
    return subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(TEST_APP),
         "-Title", title],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )  # fmt: skip


def load_tasks(path: Path = TASKS_PATH) -> list[AgentTask]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [AgentTask.model_validate(t) for t in data.get("tasks", [])]


class _Session:
    """A headless browser context as the agent's PageSource."""

    def __init__(self, context: Any) -> None:
        self.context = context

    async def pages(self) -> list[Any]:
        return [p for p in self.context.pages if not p.is_closed()] or [
            await self.context.new_page()
        ]

    async def new_page(self) -> Any:
        return await self.context.new_page()

    def open_pages(self) -> list[Any]:
        return [p for p in self.context.pages if not p.is_closed()]

    async def reset(self) -> None:
        return None


@dataclass
class TaskResult:
    task: AgentTask
    passed: bool
    failed_checks: list[str]
    outcome: AgentOutcome
    cost_usd: float
    prompts: list[str] = field(default_factory=list)


async def _silent(_: str) -> None:
    return None


def _status(outcome: AgentOutcome) -> str:
    if outcome.ok:
        return "ok"
    return {"user_declined": "declined", "ask": "ask"}.get(outcome.error or "", "fail")


def _common_checks(checks: dict[str, Any], outcome: AgentOutcome, prompts: list[str]) -> list[str]:
    failed = []
    said = checks.get("message_contains", [])
    for words in [said] if isinstance(said, str) else said:
        if words.lower() not in outcome.message.lower():
            failed.append(f"message {outcome.message!r} lacks {words!r}")
    unwanted = checks.get("no_confirm_contains", [])
    for words in [unwanted] if isinstance(unwanted, str) else unwanted:
        bad = [p for p in prompts if words in p]
        if bad:
            failed.append(f"asked to confirm {words!r}: {bad[0]!r}")
    wanted = checks.get("confirm_contains", [])
    for words in [wanted] if isinstance(wanted, str) else wanted:
        if not any(words in p for p in prompts):
            failed.append(f"never asked to confirm {words!r} (asked: {prompts!r})")
    allowed = checks.get("status")
    allowed = [allowed] if isinstance(allowed, str) else allowed
    if allowed and _status(outcome) not in allowed:
        failed.append(f"status {_status(outcome)} not in {allowed} ({outcome.message!r})")
    return failed


async def run_desktop_task(task: AgentTask, settings: Settings) -> TaskResult:
    from jarvis.agent import build_agent_planners

    session = DesktopSession()
    launcher = AppLauncher(load_aliases())
    app = launch_test_app() if task.start == "testapp" else None
    try:
        if app is not None:
            for _ in range(60):  # PowerShell + Windows Forms take ~1 s to show the window
                if await session.switch(TEST_APP_TITLE):
                    break
                await asyncio.sleep(0.1)
        confirmer = ScriptedConfirmer(list(task.answers))
        policy = SafetyPolicy.from_yaml()
        files, downloads = task_folders(task)
        desktop = DesktopExecutor(session, launcher)
        desktop.downloads_dir = downloads
        runner = ActionRunner(policy, desktop, confirmer, annotate=FileResolver([files]).annotate)
        planners = build_agent_planners(settings, Connectivity(probe=lambda: True))
        agent = TaskAgent(planners, {"desktop": DesktopSurface(session)}, runner.run_action,
                          settings.agent, trace_dir=default_trace_dir(), eyes=EYES)  # fmt: skip
        outcome = await agent.run(task.goal, "desktop")

        checks = task.success
        failed = _common_checks(checks, outcome, confirmer.prompts)
        if "window_contains" in checks or "window_lacks" in checks:
            snap = await session.observe(400, 4000)
            shown = " ".join([snap.title, snap.text, *(e.name for e in snap.elements)])
            if "window_contains" in checks and checks["window_contains"] not in shown:
                failed.append(f"window lacks {checks['window_contains']!r}")
            if "window_lacks" in checks and checks["window_lacks"] in shown:
                failed.append(f"window shows {checks['window_lacks']!r}")
    finally:
        if app is not None:
            app.kill()
        if task.close_after:
            launcher.close(task.close_after)
        session.close()

    prices = openrouter_prices().get(settings.llm.agent_model)
    return TaskResult(task, not failed, failed, outcome, cost("openrouter", outcome.usage, prices),
                      confirmer.prompts)  # fmt: skip


async def run_task(browser: Any, task: AgentTask, settings: Settings) -> TaskResult:
    from jarvis.agent import build_agent_planners

    context = await browser.new_context()
    page = await context.new_page()
    start, _, query = task.start.partition("?")  # a start page may carry a query (?c=orange)
    await page.goto(SITES.joinpath(start).as_uri() + (f"?{query}" if query else ""))
    confirmer = ScriptedConfirmer(list(task.answers))
    policy = SafetyPolicy.from_yaml()
    executor = BrowserExecutor(
        _Session(context), load_youtube_config(), policy.rules.captcha, confirmer, _silent
    )
    files, downloads = task_folders(task)
    executor.downloads_dir = downloads
    annotate = chain(executor.annotate, FileResolver([files]).annotate)
    planners = build_agent_planners(settings, Connectivity(probe=lambda: True))
    reader = OrderReader(planners, executor.visible_text)
    runner = ActionRunner(policy, executor, confirmer, annotate=annotate, order_reader=reader)
    executor.captcha.escalate = runner.run_action
    agent = BrowserAgent(planners, executor, runner.run_action, settings.agent,
                         trace_dir=default_trace_dir(), eyes=EYES)  # fmt: skip
    outcome = await agent.run(task.goal)

    page = executor.agent_page or page
    body = await page.inner_text("body") if not page.is_closed() else ""
    checks = task.success
    tab_failures = []
    if "tabs_at_least" in checks and len(context.pages) < checks["tabs_at_least"]:
        tab_failures.append(f"only {len(context.pages)} tab(s) open")
    if checks.get("private") and page.context is context:
        tab_failures.append("not in a private window")
    failed = tab_failures
    if "url_contains" in checks and checks["url_contains"] not in page.url:
        failed.append(f"url {page.url!r} lacks {checks['url_contains']!r}")
    if "body_contains" in checks and checks["body_contains"] not in body:
        failed.append(f"page lacks {checks['body_contains']!r}")
    if "body_lacks" in checks and checks["body_lacks"] in body:
        failed.append(f"page has {checks['body_lacks']!r}")
    failed += _common_checks(checks, outcome, confirmer.prompts)
    if checks.get("password_empty"):
        values = await page.eval_on_selector_all(
            "input[type=password]", "els => els.map(e => e.value)"
        )
        if any(values):
            failed.append("something was typed into a password field")
    for selector in checks.get("fields_empty", []):
        box = page.locator(selector).first
        if not page.is_closed() and await box.count() and await box.input_value():
            failed.append(f"something was typed into {selector}")
    await context.close()

    model = settings.llm.agent_model
    prices = openrouter_prices().get(model)
    return TaskResult(task, not failed, failed, outcome, cost("openrouter", outcome.usage, prices),
                      confirmer.prompts)  # fmt: skip


async def run_eval(settings: Settings, only: list[str] | None = None) -> list[TaskResult]:
    from playwright.async_api import async_playwright

    tasks = [t for t in load_tasks() if not only or t.id in only]
    results = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        for task in tasks:
            started = time.perf_counter()
            if task.where == "desktop":
                result = await run_desktop_task(task, settings)
            else:
                result = await run_task(browser, task, settings)
            mark = "PASS" if result.passed else "FAIL"
            print(f"  {mark} {task.id:26} {result.outcome.steps:2} steps "
                  f"{result.outcome.actions:2} actions {time.perf_counter() - started:5.1f} s  "
                  f"{result.outcome.message[:70]!r}")  # fmt: skip
            results.append(result)
        await browser.close()
    return results


def report(results: list[TaskResult], model: str) -> str:
    passed = sum(r.passed for r in results)
    secs = [r.outcome.ms / 1000 for r in results]
    total_cost = sum(r.cost_usd for r in results)
    lines = [
        f"# Agent benchmark — {date.today().isoformat()}",
        "",
        f"Model: `{model}` (via OpenRouter). Local test sites in `evals/sites/agent` (headless); "
        "app tasks in the test app `evals/apps/testapp.ps1` and built-in Windows apps.",
        "",
        f"**{passed}/{len(results)} tasks passed** · avg {sum(secs) / max(1, len(secs)):.1f} s · "
        f"total ${total_cost:.4f}",
        "",
        "| Task | Result | Steps | Actions | Time | Cost | Said |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        o = r.outcome
        lines.append(
            f"| {r.task.id} | {'✅' if r.passed else '❌ ' + '; '.join(r.failed_checks)} | "
            f"{o.steps} | {o.actions} | {o.ms / 1000:.1f} s | ${r.cost_usd:.4f} | {o.message} |"
        )
    return "\n".join(lines) + "\n"


def save_report(text: str) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"agent-{date.today().isoformat()}.md"
    path.write_text(text, encoding="utf-8")
    return path
