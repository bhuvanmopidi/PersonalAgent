import pytest

from jarvis.models import Intent
from jarvis.planner.evaluate import (
    Case,
    Prediction,
    TargetResult,
    load_cases,
    report,
    score,
    slot_matches,
)
from jarvis.router.regex_router import RegexRouter


@pytest.mark.parametrize(
    ("expected", "got", "ok"),
    [
        ("30", "30", True),
        ("30", "30.0", True),
        ("min", "minutes", True),
        ("seconds", "s", True),
        ("hardstyle music", "hardstyle", True),  # token-set similarity
        ("arijit singh songs", "Arijit Singh songs", True),
        ("spotify", "notepad", False),
        ("30", "50", False),
    ],
)
def test_slot_matches(expected: str, got: str, ok: bool) -> None:
    assert slot_matches(expected, got) is ok


def act(name: str, **args: str) -> Prediction:
    return Prediction("run_intent", Intent(name=name, args=args))


def test_score() -> None:
    louder = Case(text="make it louder", intent="volume_up")
    assert score(louder, act("volume_up")).correct
    assert score(louder, act("volume_down")).wrong_action  # did the wrong thing
    nothing = score(louder, Prediction("none"))
    assert not nothing.correct and not nothing.wrong_action  # unhelpful but harmless
    question = Case(text="who are you", kind="answer")
    assert score(question, Prediction("answer")).correct
    assert score(question, act("open_app", app="who")).wrong_action  # acted on a question
    hardstyle = Case(text="put on hardstyle", intent="youtube_play", slots={"query": "hardstyle"})
    assert score(hardstyle, act("play_default", query="hardstyle")).correct  # same action


def test_cases_file_is_valid_and_names_real_intents() -> None:
    cases = load_cases()
    intents = set(RegexRouter.from_yaml().specs)
    assert len(cases) >= 70
    for c in cases:
        assert (c.intent in intents) if c.kind == "run_intent" else c.intent is None, c.text


def test_report_table() -> None:
    cases = [Case(text="mute", intent="mute", tag="exact"), Case(text="hi", kind="answer", tag="q")]
    r = TargetResult("rules")
    r.alone = r.hybrid = [score(cases[0], act("mute")), score(cases[1], Prediction("none"))]
    r.ms = [0.1, 0.1]
    text = report([r], cases)
    assert "| rules | 50% | 50% | 0% | 0% |" in text and "## Misses — rules" in text


@pytest.mark.parametrize(
    "text",
    ["open it", "close that", "close it now", "play this one", "play it a bit quicker",
     "launch that insta thing", "start this video from the beginning"],
)  # fmt: skip
def test_rules_never_act_on_a_bare_pronoun(text: str) -> None:
    assert RegexRouter.from_yaml().route(text) is None  # the LLM resolves it, or asks


def test_cost_per_provider() -> None:
    from jarvis.planner.evaluate import cost
    from jarvis.planner.llm import Usage

    one_m = Usage(input_tokens=1_000_000, cache_read_tokens=1_000_000, output_tokens=1_000_000)
    assert cost("openai", one_m) == pytest.approx(0.15 + 0.075 + 0.60)  # gpt-4o-mini
    assert cost("local", one_m) == 0.0


def test_sample_per_tag_keeps_every_category() -> None:
    from jarvis.planner.evaluate import load_cases, sample_per_tag

    cases = load_cases()
    picked = sample_per_tag(cases, 3)
    tags = {c.tag for c in cases}
    assert {c.tag for c in picked} == tags and len(picked) <= 3 * len(tags)
