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
        ("open notepad", "open_app", {"app": "notepad", "prefer": "auto"}),
        ("Hey Jarvis, please open Spotify!", "open_app", {"app": "spotify", "prefer": "auto"}),
        ("launch the chrome app", "open_app", {"app": "chrome", "prefer": "app"}),
        ("go to github.com", "open_app", {"app": "github.com", "prefer": "auto"}),
        ("open youtube", "open_app", {"app": "youtube", "prefer": "auto"}),
        ("open me instagram", "open_app", {"app": "instagram", "prefer": "auto"}),
        ("open up the whatsapp website", "open_app", {"app": "whatsapp", "prefer": "website"}),
        ("open my spotify app", "open_app", {"app": "spotify", "prefer": "app"}),
        ("open the app store", "open_app", {"app": "app store", "prefer": "auto"}),
        ("open downloads", "open_folder", {"folder": "downloads"}),
        ("open my documents folder", "open_folder", {"folder": "documents"}),
        ("close notepad", "close_app", {"app": "notepad"}),
        ("quit the spotify app", "close_app", {"app": "spotify"}),
        ("close my notepad", "close_app", {"app": "notepad"}),
        ("play lofi on youtube", "youtube_play", {"query": "lofi"}),
        ("put on arijit singh songs on youtube", "youtube_play", {"query": "arijit singh songs"}),
        ("search lofi on youtube", "youtube_search", {"query": "lofi"}),
        ("play a hardstylemusic in youtube", "youtube_play", {"query": "hardstylemusic"}),
        ("play me a song on yt", "youtube_play", {"query": "song"}),
        ("watch mr beast on youtube", "youtube_play", {"query": "mr beast"}),
        ("play a hardstyle music", "play_default", {"query": "hardstyle music"}),
        ("play some lofi", "play_default", {"query": "lofi"}),
        ("put on arijit singh songs", "play_default", {"query": "arijit singh songs"}),
        ("play it again", "media_resume", {}),  # not play_default("it again")
        ("play the next video", "next_video", {}),
        ("fast forward by 10 min", "media_forward", {"amount": "10", "unit": "min"}),
        ("fast forward", "media_forward", {"amount": "10", "unit": "seconds"}),
        ("skip 30 seconds", "media_forward", {"amount": "30", "unit": "seconds"}),  # not next_video
        ("rewind", "media_rewind", {"amount": "10", "unit": "seconds"}),
        ("go back 1 minute", "media_rewind", {"amount": "1", "unit": "minute"}),  # not go_back
        ("go to 5:30", "media_seek_to", {"time": "5:30"}),
        ("jump to 10 minutes", "media_seek_to", {"amount": "10", "unit": "minutes"}),
        ("start over", "media_restart", {}),
        ("speed 1.5x", "media_speed", {"rate": "1.5"}),
        ("slow down", "media_slower", {}),
        ("normal speed", "media_normal_speed", {}),
        ("pause", "media_pause", {}),
        ("pause the video", "media_pause", {}),
        ("resume", "media_resume", {}),
        ("play", "media_resume", {}),
        ("next video", "next_video", {}),
        ("skip", "next_video", {}),
        ("skip the ad", "skip_ad", {}),
        ("full screen", "fullscreen", {}),
        ("exit full screen", "fullscreen", {}),  # not close_app("full screen")
        ("always skip ads", "ad_policy", {"mode": "always"}),
        ("never skip ads", "ad_policy", {"mode": "never"}),
        ("ask me about ads", "ad_policy", {"mode": "ask"}),
        ("go back", "go_back", {}),
        ("refresh the page", "reload", {}),
        ("open a new tab", "new_tab", {}),  # not open_app("a new tab")
        ("close this tab", "close_tab", {}),
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


@pytest.mark.parametrize("text", ["play blinding lights on spotify"])
def test_spotify_requests_are_not_played_on_youtube(router: RegexRouter, text: str) -> None:
    assert router.route(text) is None  # until Phase 9: never the wrong app
