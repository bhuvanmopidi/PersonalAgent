from typing import Any

import pytest

from jarvis.executors.base import ActionRunner
from jarvis.models import Action, ActionResult
from jarvis.safety.confirm import ScriptedConfirmer
from jarvis.safety.policy import SafetyPolicy


class RecordingExecutor:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[Action] = []
        self.fail = fail

    async def execute(self, action: Action) -> ActionResult:
        self.calls.append(action)
        if self.fail:
            raise RuntimeError("window not found")
        return ActionResult(ok=True, observation="done")


def make(
    answers: list[bool] | None = None, handoff_done: bool = True, fail: bool = False
) -> tuple[ActionRunner, RecordingExecutor, ScriptedConfirmer]:
    executor = RecordingExecutor(fail)
    confirmer = ScriptedConfirmer(answers, handoff_done)
    return ActionRunner(SafetyPolicy.from_yaml(), executor, confirmer), executor, confirmer


async def test_annotator_runs_before_the_policy() -> None:
    executor, confirmer = RecordingExecutor(), ScriptedConfirmer([False])

    async def on_checkout_page(action: Action) -> Action:
        return action.model_copy(update={"page_url": "https://shop.example/checkout/review"})

    runner = ActionRunner(SafetyPolicy.from_yaml(), executor, confirmer, annotate=on_checkout_page)
    result = await runner.run_action(Action(kind="media_key", target="pause"))
    assert (
        result.error == "order_details_missing" and executor.calls == []
    )  # PAYMENT from the page context


async def test_green_runs_without_asking() -> None:
    runner, executor, confirmer = make()
    result = await runner.run_action(Action(kind="launch_app", target="notepad"))
    assert result.ok and len(executor.calls) == 1 and confirmer.prompts == []
    assert result.duration_ms >= 0


async def test_yellow_declined_never_executes() -> None:
    runner, executor, confirmer = make(answers=[False])
    action = Action(kind="send", description="Send email to arjun@gmail.com")
    result = await runner.run_action(action)
    assert result.error == "user_declined" and executor.calls == []
    assert "arjun@gmail.com" in confirmer.prompts[0]


async def test_yellow_confirmed_executes() -> None:
    runner, executor, _ = make(answers=[True])
    result = await runner.run_action(Action(kind="send"))
    assert result.ok and len(executor.calls) == 1


async def test_payment_prompt_reads_back_everything(full_payment: dict[str, Any]) -> None:
    runner, _, confirmer = make(answers=[True])
    result = await runner.run_action(Action(kind="pay", args=full_payment))
    assert result.ok
    for value in ("boAt Airdopes 141", "Rs 1,299", "Home", "saved HDFC card"):
        assert value in confirmer.prompts[0]


async def test_incomplete_payment_refused_without_asking() -> None:
    runner, executor, confirmer = make(answers=[True])
    result = await runner.run_action(Action(kind="pay", args={"price": "Rs 99"}))
    assert (
        result.error == "order_details_missing" and executor.calls == [] and confirmer.prompts == []
    )


@pytest.mark.parametrize(("done", "ok"), [(True, True), (False, False)])
async def test_red_is_handed_off_never_executed(done: bool, ok: bool) -> None:
    runner, executor, confirmer = make(handoff_done=done)
    result = await runner.run_action(Action(kind="enter_otp", description="Enter the OTP"))
    assert result.ok is ok and executor.calls == []
    assert "Enter the OTP" in confirmer.prompts[0]


async def test_executor_exception_becomes_result() -> None:
    runner, _, _ = make(fail=True)
    result = await runner.run_action(Action(kind="launch_app"))
    assert not result.ok and result.error is not None and "window not found" in result.error


class SilentSpeaker:
    async def say(self, text: str, wait: bool = False) -> None:
        return None


def test_confirmers_say_whether_a_question_can_be_abandoned() -> None:
    from jarvis.safety.confirm import SpokenConfirmer, TerminalConfirmer, can_cancel

    assert can_cancel(ScriptedConfirmer())
    assert not can_cancel(TerminalConfirmer())  # its input() thread can't be stopped
    assert not can_cancel(SpokenConfirmer(TerminalConfirmer(), SilentSpeaker()))
    assert can_cancel(SpokenConfirmer(ScriptedConfirmer(), SilentSpeaker()))


async def test_handoff_message_is_plain_words() -> None:
    runner, _, confirmer = make()
    await runner.run_action(Action(kind="enter_otp", description="enter the OTP"))
    assert (
        confirmer.prompts[0]
        == "I need you for this step: enter the OTP. Say done when you've finished."
    )
