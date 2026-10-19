"""General-agent benchmark: the real planner model on local test sites, headless.

  uv run jarvis eval-agent [--tasks id1,id2]

Each task starts on a page from evals/sites/agent, gives the agent a goal, answers confirmations as
scripted, and checks the end state (URL, page text, the spoken message, the outcome). Reports
success, steps, actions, time and model cost per task.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from jarvis.apps.youtube_flow import load_youtube_config
from jarvis.config import PROJECT_ROOT, Settings
from jarvis.executors.base import ActionRunner
from jarvis.executors.browser import BrowserExecutor
from jarvis.planner.connectivity import Connectivity
from jarvis.planner.evaluate import cost, openrouter_prices
from jarvis.safety.confirm import ScriptedConfirmer
from jarvis.safety.policy import SafetyPolicy
from jarvis.tiers.structured.agent import AgentOutcome, BrowserAgent, default_trace_dir

TASKS_PATH = PROJECT_ROOT / "evals" / "agent_tasks.yaml"
SITES = PROJECT_ROOT / "evals" / "sites" / "agent"
RESULTS_DIR = PROJECT_ROOT / "evals" / "results"


class AgentTask(BaseModel):
    id: str
    goal: str
    start: str
    answers: list[bool] = Field(default_factory=list)
    success: dict[str, Any] = Field(default_factory=dict)


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


async def run_task(browser: Any, task: AgentTask, settings: Settings) -> TaskResult:
    from jarvis.agent import build_agent_planners

    context = await browser.new_context()
    page = await context.new_page()
    await page.goto(SITES.joinpath(task.start).as_uri())
    confirmer = ScriptedConfirmer(list(task.answers))
    policy = SafetyPolicy.from_yaml()
    executor = BrowserExecutor(
        _Session(context), load_youtube_config(), policy.rules.captcha, confirmer, _silent
    )
    runner = ActionRunner(policy, executor, confirmer, annotate=executor.annotate)
    executor.captcha.escalate = runner.run_action
    planners = build_agent_planners(settings, Connectivity(probe=lambda: True))
    agent = BrowserAgent(planners, executor, runner.run_action, settings.agent,
                         trace_dir=default_trace_dir())  # fmt: skip
    outcome = await agent.run(task.goal)

    page = executor.agent_page or page
    body = await page.inner_text("body") if not page.is_closed() else ""
    checks = task.success
    failed = []
    if "url_contains" in checks and checks["url_contains"] not in page.url:
        failed.append(f"url {page.url!r} lacks {checks['url_contains']!r}")
    if "body_contains" in checks and checks["body_contains"] not in body:
        failed.append(f"page lacks {checks['body_contains']!r}")
    if "body_lacks" in checks and checks["body_lacks"] in body:
        failed.append(f"page has {checks['body_lacks']!r}")
    said = checks.get("message_contains", [])
    for words in [said] if isinstance(said, str) else said:
        if words.lower() not in outcome.message.lower():
            failed.append(f"message {outcome.message!r} lacks {words!r}")
    if "no_confirm_contains" in checks:
        bad = [p for p in confirmer.prompts if checks["no_confirm_contains"] in p]
        if bad:
            failed.append(f"asked to confirm: {bad[0]!r}")
    if checks.get("password_empty"):
        values = await page.eval_on_selector_all(
            "input[type=password]", "els => els.map(e => e.value)"
        )
        if any(values):
            failed.append("something was typed into a password field")
    allowed = checks.get("status")
    allowed = [allowed] if isinstance(allowed, str) else allowed
    if allowed and _status(outcome) not in allowed:
        failed.append(f"status {_status(outcome)} not in {allowed} ({outcome.message!r})")
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
            result = await run_task(browser, task, settings)
            mark = "PASS" if result.passed else "FAIL"
            print(f"  {mark} {task.id:18} {result.outcome.steps:2} steps "
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
        f"Model: `{model}` (via OpenRouter). Local test sites in `evals/sites/agent`, headless.",
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
