import pytest

from jarvis.router.regex_router import RegexRouter, normalize


@pytest.fixture(scope="module")
def router() -> RegexRouter:
    return RegexRouter.from_yaml()  # the real config/intents.yaml


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Hey Jarvis, please open Spotify!", "open spotify"),
        ("  Open   Notepad. ", "open notepad"),
        ("can you please mute for me", "mute"),
        ("go to github.com", "go to github.com"),
    ],
)
def test_normalize(text: str, expected: str) -> None:
    assert normalize(text) == expected


@pytest.mark.parametrize(
    ("text", "name", "args"),
    [
        ("open notepad", "open_app", {"app": "notepad"}),
        ("Hey Jarvis, please open Spotify!", "open_app", {"app": "spotify"}),
        ("launch the chrome app", "open_app", {"app": "chrome"}),
        ("go to github.com", "open_app", {"app": "github.com"}),
        ("open youtube", "open_app", {"app": "youtube"}),
        ("open downloads", "open_folder", {"folder": "downloads"}),
        ("open my documents folder", "open_folder", {"folder": "documents"}),
        ("close notepad", "close_app", {"app": "notepad"}),
        ("quit the spotify app", "close_app", {"app": "spotify"}),
        ("play lofi on youtube", "youtube_search", {"query": "lofi"}),
        ("search youtube for blinding lights", "youtube_search", {"query": "blinding lights"}),
        ("youtube arijit singh songs", "youtube_search", {"query": "arijit singh songs"}),
        ("google best earbuds under 2000", "web_search", {"query": "best earbuds under 2000"}),
        ("search for weather in hyderabad", "web_search", {"query": "weather in hyderabad"}),
        ("volume up", "volume_up", {"value": "10"}),
        ("turn the volume up by 20", "volume_up", {"value": "20"}),
        ("lower the volume", "volume_down", {"value": "10"}),
        ("volume 30", "volume_set", {"value": "30"}),
        ("set volume to 45%", "volume_set", {"value": "45"}),
        ("mute", "mute", {}),
        ("unmute the sound", "unmute", {}),
        ("brightness 50", "brightness_set", {"value": "50"}),
        ("dim the brightness", "brightness_down", {"value": "10"}),
        ("brighter", "brightness_up", {"value": "10"}),
        ("find my resume", "find_file", {"query": "resume"}),
        ("where is the project report file", "find_file", {"query": "project report"}),
        ("lock the screen", "lock", {}),
        ("lock", "lock", {}),
    ],
)
def test_routes(router: RegexRouter, text: str, name: str, args: dict[str, str]) -> None:
    intent = router.route(text)
    assert intent is not None, f"no match for {text!r}"
    assert (intent.name, intent.args) == (name, args)
    assert intent.source == "regex" and intent.confidence == 1.0


@pytest.mark.parametrize(
    "text", ["send it to him", "what is two plus two", "", "order airdopes from amazon"]
)
def test_no_match(router: RegexRouter, text: str) -> None:
    assert router.route(text) is None


def test_every_intent_has_an_action_kind(router: RegexRouter) -> None:
    assert all(spec.action.kind for spec in router.specs.values())
