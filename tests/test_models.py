import pytest
from pydantic import ValidationError

from jarvis.models import Action, ActionResult, Intent, RiskLevel


def test_risk_levels_are_ordered() -> None:
    assert RiskLevel.GREEN < RiskLevel.YELLOW < RiskLevel.PAYMENT < RiskLevel.RED


def test_action_defaults_are_independent() -> None:
    a, b = Action(kind="click"), Action(kind="click")
    a.args["x"] = 1
    a.flags.add("password_field")
    assert b.args == {} and b.flags == set()


def test_intent_confidence_bounds() -> None:
    with pytest.raises(ValidationError):
        Intent(name="open_app", confidence=1.5)


def test_action_result_shape() -> None:
    r = ActionResult(ok=False, error="killed")
    assert r.observation == "" and r.duration_ms == 0.0
