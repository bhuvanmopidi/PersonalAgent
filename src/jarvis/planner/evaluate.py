"""Router benchmark: rules vs Claude vs local model — accuracy, wrong actions, speed, cost.

  uv run jarvis eval-router --providers rules,openrouter,local [--openrouter-models a,b]
                            [--local-models qwen2.5:3b]

For every case it scores: the model alone, and hybrid (rules first, the model for what they miss —
what Jarvis actually does). "Wrong action" = it ran an intent when it shouldn't have, or the wrong
one: the most important number, because that's a real thing done on your PC.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal, cast

import yaml
from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from jarvis.config import PROJECT_ROOT, Settings
from jarvis.models import Intent
from jarvis.planner.catalog import Catalog
from jarvis.planner.connectivity import Connectivity
from jarvis.planner.llm import (
    ClaudeProvider,
    LLMProvider,
    OllamaProvider,
    OpenAIProvider,
    OpenRouterProvider,
    Usage,
)
from jarvis.planner.router import LLMRouter, ProviderName, Routed
from jarvis.router.regex_router import RegexRouter

CASES_PATH = PROJECT_ROOT / "evals" / "router_cases.yaml"
RESULTS_DIR = PROJECT_ROOT / "evals" / "results"

# USD per 1M tokens: (input, cached input, cache write, output).
# Claude Haiku 5.5 (Anthropic, Oct 2026): cache reads 0.1x input, 5-minute cache writes 1.25x input.
# gpt-4o-mini (OpenAI): cached input at half price, no cache-write charge. OpenRouter :free = $0.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude": (0.10, 0.01, 0.125, 0.50),
    "openai": (0.15, 0.075, 0.0, 0.60),
}


class Case(BaseModel):
    text: str
    kind: Literal["run_intent", "task", "answer", "clarify", "unsupported"] = "run_intent"
    intent: str | None = None
    slots: dict[str, str] = Field(default_factory=dict)
    goal_has: list[str] = Field(default_factory=list)  # task: words its goal must contain
    where: Literal["browser", "desktop"] | None = None  # task: website or Windows app
    context: str | None = None
    context_did: str = "(done)"  # what Jarvis did/said for `context` (e.g. asked a question)
    tag: str = ""


def sample_per_tag(cases: list[Case], n: int) -> list[Case]:
    """The first n cases of each category, in file order (stable between runs)."""
    seen: dict[str, int] = {}
    picked = []
    for case in cases:
        if seen.get(case.tag, 0) < n:
            seen[case.tag] = seen.get(case.tag, 0) + 1
            picked.append(case)
    return picked


def load_cases(path: Path = CASES_PATH) -> list[Case]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [Case.model_validate(c) for c in data.get("cases", [])]


@dataclass
class Prediction:
    kind: str  # run_intent | answer | clarify | unsupported | none (rules didn't match / error)
    intent: Intent | None = None
    ms: float = 0.0
    usage: Usage = field(default_factory=Usage)
    error: str | None = None
    goal: str = ""  # kind "task": what the agent would be asked to do
    where: str = ""  # kind "task": browser or desktop


def slot_matches(expected: str, got: str) -> bool:
    e, g = expected.strip().lower(), got.strip().lower()
    try:
        return float(e) == float(g)
    except ValueError:
        pass
    if e in {"min", "mins", "minute", "minutes"} and g in {"min", "mins", "minute", "minutes"}:
        return True
    if e in {"s", "sec", "secs", "second", "seconds"} and g in {
        "s",
        "sec",
        "secs",
        "second",
        "seconds",
    }:
        return True
    return bool(fuzz.token_set_ratio(e, g) >= 80)


@dataclass
class Score:
    correct: bool
    wrong_action: bool  # acted when it shouldn't, or did the wrong thing


# rules-only intents that do exactly the same as another intent
SAME_AS = {"play_default": "youtube_play"}


def score(case: Case, pred: Prediction) -> Score:
    acted = pred.kind == "run_intent" and pred.intent is not None
    if case.kind == "task":
        goal = pred.goal.lower()
        right = pred.kind == "task" and all(w.lower() in goal for w in case.goal_has)
        right = right and (case.where is None or pred.where == case.where)
        return Score(correct=right, wrong_action=acted)
    if case.kind != "run_intent":
        return Score(correct=pred.kind == case.kind, wrong_action=acted)
    if not acted:
        return Score(correct=False, wrong_action=False)  # did nothing: unhelpful but harmless
    assert pred.intent is not None
    name = SAME_AS.get(pred.intent.name, pred.intent.name)
    right = name == case.intent and all(
        slot_matches(v, str(pred.intent.args.get(k, ""))) for k, v in case.slots.items()
    )
    return Score(correct=right, wrong_action=not right)


def rules_predict(
    rules: RegexRouter, case: Case, resolves: Callable[[str, str], bool] | None = None
) -> Prediction:
    """What the rules do — like Jarvis: an "open X" whose X is no app or site is left to the LLM."""
    start = time.perf_counter()
    intent = rules.route(case.text)
    is_open = intent is not None and intent.name == "open_app" and resolves is not None
    if is_open and intent is not None and resolves is not None:
        app, prefer = str(intent.args.get("app", "")), str(intent.args.get("prefer", ""))
        if not resolves(app, prefer):
            intent = None
    ms = (time.perf_counter() - start) * 1000
    return Prediction("run_intent" if intent else "none", intent, ms)


def app_resolver() -> Callable[[str, str], bool]:
    """This PC's apps and sites (the same AppLauncher Jarvis uses)."""
    from jarvis.tiers.instant.apps import AppLauncher, load_aliases, parse_prefer

    launcher = AppLauncher(load_aliases())
    return lambda app, prefer: launcher.resolve(app, parse_prefer(prefer)) is not None


async def model_predict(router: LLMRouter, case: Case) -> Prediction:
    context = f'- user: "{case.context}" -> {case.context_did}' if case.context else "(none)"
    routed: Routed = await router.route(case.text, context)
    if routed.decision is None:
        return Prediction("none", None, routed.ms, routed.usage, routed.error)
    d = routed.decision
    return Prediction(d.kind, routed.intent, routed.ms, routed.usage, goal=d.goal or "",
                      where=d.where or "")  # fmt: skip


def openrouter_prices() -> dict[str, tuple[float, float, float, float]]:
    """Live per-model prices from OpenRouter's public model list (USD per 1M tokens)."""
    import json
    import urllib.request

    try:
        with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=15) as r:
            models = json.load(r)["data"]
    except OSError:
        return {}
    prices = {}
    for m in models:
        p = m.get("pricing") or {}
        inp, out = float(p.get("prompt") or 0) * 1e6, float(p.get("completion") or 0) * 1e6
        cached = float(p.get("input_cache_read") or 0) * 1e6 or inp
        prices[m["id"]] = (inp, cached, 0.0, out)
    return prices


def cost(
    provider: str, usage: Usage, prices: tuple[float, float, float, float] | None = None
) -> float:
    inp, cached, write, out = prices or PRICES.get(provider, (0.0, 0.0, 0.0, 0.0))
    return (
        usage.input_tokens * inp
        + usage.cache_read_tokens * cached
        + usage.cache_write_tokens * write
        + usage.output_tokens * out
    ) / 1_000_000


@dataclass
class TargetResult:
    name: str
    alone: list[Score] = field(default_factory=list)
    hybrid: list[Score] = field(default_factory=list)
    ms: list[float] = field(default_factory=list)  # model calls only
    cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)

    def pct(self, scores: list[Score], attr: Literal["correct", "wrong_action"]) -> float:
        hits = sum(1 for s in scores if (s.correct if attr == "correct" else s.wrong_action))
        return 100 * hits / max(1, len(scores))


def _p(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def describe(pred: Prediction) -> str:
    if pred.intent is not None:
        args = {k: v for k, v in pred.intent.args.items() if v}
        return f"{pred.intent.name} {args}"
    return pred.kind + (f" ({pred.error})" if pred.error else "")


async def run_eval(
    settings: Settings,
    targets: list[str],
    cases: list[Case],
    progress: Callable[[str], None] = print,
) -> list[TargetResult]:
    rules = RegexRouter.from_yaml()
    catalog = Catalog(rules.specs.values())
    resolves = app_resolver()
    rule_preds = [rules_predict(rules, c, resolves) for c in cases]
    results = []
    for target in targets:
        result = TargetResult(target)
        if target == "rules":
            for case, pred in zip(cases, rule_preds, strict=True):
                s = score(case, pred)
                result.alone.append(s)
                result.hybrid.append(s)
                result.ms.append(pred.ms)
                if not s.correct:
                    result.misses.append(f"{case.text!r} → {describe(pred)}")
            results.append(result)
            continue

        provider: LLMProvider
        prices: tuple[float, float, float, float] | None = None
        if target.startswith("openrouter"):
            model = target.split(":", 1)[1] if ":" in target else settings.llm.openrouter_model
            provider = OpenRouterProvider(
                settings.llm.model_copy(update={"openrouter_model": model})
            )
            prices = openrouter_prices().get(model)
        elif target == "openai":
            provider = OpenAIProvider(settings.llm)
        elif target == "claude":
            provider = ClaudeProvider(settings.llm)
        else:  # "local:<model>"
            model = target.split(":", 1)[1]
            provider = OllamaProvider(settings.llm.model_copy(update={"local_model": model}))
        if isinstance(provider, OllamaProvider):
            started = time.perf_counter()
            warmed = await provider.warm()
            progress(f"  {target}: model {'loaded' if warmed else 'NOT loaded'} "
                     f"in {time.perf_counter() - started:.1f} s (not counted)")  # fmt: skip
        router = LLMRouter(catalog, {provider.name: provider}, Connectivity(probe=lambda: True),
                           cast(ProviderName, provider.name))  # fmt: skip
        for i, (case, rule_pred) in enumerate(zip(cases, rule_preds, strict=True), 1):
            pred = await model_predict(router, case)
            progress(f"  {target} {i}/{len(cases)}  {pred.ms:5.0f} ms  {case.text}")
            if pred.error:
                result.errors.append(f"{case.text!r}: {pred.error}")
            alone = score(case, pred)
            result.alone.append(alone)
            hybrid_pred = rule_pred if rule_pred.intent is not None else pred
            result.hybrid.append(score(case, hybrid_pred))
            result.ms.append(pred.ms)
            result.cost_usd += cost(provider.name, pred.usage, prices)
            if not alone.correct:
                result.misses.append(f"{case.text!r} → {describe(pred)}")
        results.append(result)
    return results


def report(results: list[TargetResult], cases: list[Case]) -> str:
    lines = [
        f"# Router benchmark — {date.today().isoformat()}",
        "",
        f"{len(cases)} cases from `evals/router_cases.yaml`. *Alone* = the model answers every"
        " case; *hybrid* = rules first, the model only for what they miss (what Jarvis does).",
        "",
        "| Target | Correct (alone) | Correct (hybrid) | Wrong actions (alone)"
        " | Wrong actions (hybrid) | p50 | p95 | $ / 1000 commands |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        per_1000 = r.cost_usd / max(1, len(cases)) * 1000
        lines.append(
            f"| {r.name} | {r.pct(r.alone, 'correct'):.0f}% | {r.pct(r.hybrid, 'correct'):.0f}% "
            f"| {r.pct(r.alone, 'wrong_action'):.0f}% | {r.pct(r.hybrid, 'wrong_action'):.0f}% "
            f"| {statistics.median(r.ms) if r.ms else 0:.0f} ms | {_p(r.ms, 0.95):.0f} ms "
            f"| ${per_1000:.3f} |"
        )
    tags = sorted({c.tag for c in cases})
    lines += ["", "## Correct by category (alone)", "",
              "| Target | " + " | ".join(tags) + " |", "|---|" + "---|" * len(tags)]  # fmt: skip
    for r in results:
        cells = []
        for tag in tags:
            idx = [i for i, c in enumerate(cases) if c.tag == tag]
            cells.append(f"{100 * sum(r.alone[i].correct for i in idx) / len(idx):.0f}%")
        lines.append(f"| {r.name} | " + " | ".join(cells) + " |")
    for r in results:
        lines += ["", f"## Misses — {r.name}", ""]
        lines += [f"- {m}" for m in r.misses] or ["- none"]
        if r.errors:
            lines += ["", f"Errors ({len(r.errors)}): {r.errors[0]}"]
    return "\n".join(lines) + "\n"


def save_report(text: str) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"router-{date.today().isoformat()}.md"
    path.write_text(text, encoding="utf-8")
    return path
