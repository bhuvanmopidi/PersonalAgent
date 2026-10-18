import pytest

from jarvis.voice.replies import is_stop, parse_handoff, parse_yes_no


@pytest.mark.parametrize(
    ("text", "answer"),
    [
        ("Yes.", True),
        ("yeah go ahead", True),
        ("Okay, send it.", True),
        ("haan", True),
        ("No.", False),
        ("nope", False),
        ("cancel", False),
        ("don't", False),
    ],
)
def test_yes_no(text: str, answer: bool) -> None:
    assert parse_yes_no(text) is answer


# both yes and no words, or neither → unclear → Jarvis asks again (and defaults to no)
@pytest.mark.parametrize("text", ["", "what", "yes no", "uh maybe", "don't send it"])
def test_yes_no_unclear(text: str) -> None:
    assert parse_yes_no(text) is None


@pytest.mark.parametrize(
    ("text", "answer"),
    [("Done.", True), ("I'm done", True), ("continue", True), ("cancel", False), ("hmm", None)],
)
def test_handoff(text: str, answer: bool | None) -> None:
    assert parse_handoff(text) is answer


@pytest.mark.parametrize("text", ["Stop.", "stop it", "Hey Jarvis, stop!", "cancel", "never mind"])
def test_stop(text: str) -> None:
    assert is_stop(text)


@pytest.mark.parametrize("text", ["stop the music on spotify", "open stopwatch", "yes"])
def test_not_stop(text: str) -> None:
    assert not is_stop(text)
