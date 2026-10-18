from typing import Any

import pytest

from jarvis.models import Action, RiskLevel
from jarvis.safety.policy import Decision, SafetyPolicy


@pytest.fixture(scope="module")
def policy() -> SafetyPolicy:
    return SafetyPolicy.from_yaml()  # the real config/safety.yaml


@pytest.mark.parametrize(
    ("action", "risk", "decision"),
    [
        (Action(kind="launch_app", target="spotify"), RiskLevel.GREEN, Decision.ALLOW),
        (Action(kind="open_url", target="youtube.com"), RiskLevel.GREEN, Decision.ALLOW),
        (Action(kind="send", target="Gmail"), RiskLevel.YELLOW, Decision.CONFIRM),
        (Action(kind="delete_file", target="a.txt"), RiskLevel.YELLOW, Decision.CONFIRM),
        (Action(kind="enter_otp"), RiskLevel.RED, Decision.HANDOFF),
        (Action(kind="login", target="Gmail"), RiskLevel.RED, Decision.HANDOFF),
    ],
)
def test_kind_rules(
    policy: SafetyPolicy, action: Action, risk: RiskLevel, decision: Decision
) -> None:
    result = policy.check(action)
    assert (result.risk, result.decision) == (risk, decision)


def test_unknown_kind_defaults_to_confirm(policy: SafetyPolicy) -> None:
    result = policy.check(Action(kind="format_disk"))
    assert result.decision == Decision.CONFIRM
    assert "unknown kind" in result.reasons[0]


@pytest.mark.parametrize(
    ("target", "risk"),
    [
        ("Send", RiskLevel.YELLOW),
        ("Delete forever", RiskLevel.YELLOW),
        ("Buy Now", RiskLevel.YELLOW),
        ("Proceed to Pay", RiskLevel.PAYMENT),
        ("Sign in", RiskLevel.RED),
        ("Verify you are human", RiskLevel.RED),
        ("Search", RiskLevel.GREEN),
        ("Sender settings", RiskLevel.GREEN),  # whole words only: "send" ≠ "sender"
    ],
)
def test_click_target_keywords_raise_risk(
    policy: SafetyPolicy, target: str, risk: RiskLevel
) -> None:
    assert policy.classify(Action(kind="click", target=target))[0] == risk


def test_typed_content_does_not_trigger_keywords(policy: SafetyPolicy) -> None:
    # dictating "send the password" into Word must not need confirmation
    action = Action(kind="type_text", target="Document", args={"text": "send the password"})
    assert policy.check(action).decision == Decision.ALLOW


@pytest.mark.parametrize(
    "target",
    ["Cancel Membership", "Unsubscribe", "Deactivate account", "Confirm", "Delete account"],
)
def test_irreversible_labels_need_confirm(policy: SafetyPolicy, target: str) -> None:
    assert policy.check(Action(kind="click", target=target)).decision == Decision.CONFIRM


def test_user_built_url_does_not_trigger_keywords(policy: SafetyPolicy) -> None:
    # "google how to delete my account" builds a URL containing "delete" — still just a search
    action = Action(kind="open_url", target="https://www.google.com/search?q=delete+my+account")
    assert policy.check(action).decision == Decision.ALLOW


def test_unknown_kind_still_checks_keywords(policy: SafetyPolicy) -> None:
    result = policy.check(Action(kind="mystery", target="Enter password"))
    assert result.decision == Decision.HANDOFF


def test_flags_raise_risk(policy: SafetyPolicy) -> None:
    action = Action(kind="type_text", target="Field", flags={"password_field"})
    assert policy.check(action).decision == Decision.HANDOFF


def test_risk_never_goes_down(policy: SafetyPolicy) -> None:
    # red kind with a harmless target stays red
    assert policy.classify(Action(kind="enter_password", target="Search"))[0] == RiskLevel.RED


def test_payment_with_full_details_needs_confirm(
    policy: SafetyPolicy, full_payment: dict[str, Any]
) -> None:
    result = policy.check(Action(kind="pay", target="Pay now", args=full_payment))
    assert result.decision == Decision.CONFIRM


def test_payment_with_missing_details_is_refused(policy: SafetyPolicy) -> None:
    result = policy.check(Action(kind="pay", args={"price": "Rs 99"}))
    assert result.decision == Decision.REFUSE
    assert set(result.missing) == {"item", "quantity", "address", "payment_method"}


def test_payment_button_click_without_details_is_refused(policy: SafetyPolicy) -> None:
    result = policy.check(Action(kind="click", target="Proceed to Pay"))
    assert result.decision == Decision.REFUSE
