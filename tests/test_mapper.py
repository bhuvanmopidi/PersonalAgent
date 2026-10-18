from jarvis.router.regex_router import RegexRouter
from jarvis.tiers.instant.mapper import render, to_action


def test_render_plain_and_url() -> None:
    slots = {"query": "lofi & chill"}
    assert render("{query}", slots) == "lofi & chill"
    assert render("q={query|url}", slots) == "q=lofi+%26+chill"
    assert render("{missing}", slots) == ""


def test_youtube_search_action() -> None:
    router = RegexRouter.from_yaml()
    intent = router.route("play blinding lights on youtube")
    assert intent is not None
    action = to_action(router.specs[intent.name], intent)
    assert action.kind == "open_url"
    assert action.target == "https://www.youtube.com/results?search_query=blinding+lights"
    assert action.description == "Searching YouTube for blinding lights"


def test_volume_action_uses_default() -> None:
    router = RegexRouter.from_yaml()
    intent = router.route("volume up")
    assert intent is not None
    action = to_action(router.specs[intent.name], intent)
    assert (action.kind, action.args) == ("set_volume", {"mode": "up", "value": "10"})
