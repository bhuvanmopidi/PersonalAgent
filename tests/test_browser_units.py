"""Browser pieces that need no browser: video picking, URL risk, routing, session helpers."""

from pathlib import Path

import pytest

from jarvis.apps.youtube_flow import Candidate, YouTubeConfig, load_youtube_config, pick_video
from jarvis.config import BrowserSettings
from jarvis.executors.browser_session import BrowserSession, BrowserUnavailable, chrome_args
from jarvis.executors.router import KindRouter
from jarvis.models import Action, ActionResult, RiskLevel
from jarvis.safety.policy import SafetyPolicy, url_risk


@pytest.fixture(scope="module")
def cfg() -> YouTubeConfig:
    return load_youtube_config()


def cand(
    index: int,
    title: str = "Video",
    href: str = "/watch?v=x",
    ad: bool = False,
    badges: tuple[str, ...] = (),
    duration: str = "3:25",
) -> Candidate:
    return Candidate(
        index=index,
        title=title,
        href=href,
        skipped_container=ad,
        badges=list(badges),
        duration=duration,
    )


def test_pick_skips_ads_shorts_live_and_mixes(cfg: YouTubeConfig) -> None:
    candidates = [
        cand(0, ad=True),
        cand(1, href="/shorts/abc"),
        cand(2, badges=("LIVE",)),
        cand(3, href="/live/abc"),
        cand(6, duration=""),  # 24/7 stream with no LIVE badge: no duration gives it away
        cand(4, title=""),
        cand(5, title="The one", href="/watch?v=abc&list=RDabc&"),  # YouTube's auto radio queue
    ]
    choice = pick_video(candidates, cfg)
    assert choice is not None and choice.index == 5


def test_pick_relaxes_duration_only_when_asked(cfg: YouTubeConfig) -> None:
    no_labels = [cand(0, duration=""), cand(1, duration="")]
    assert pick_video(no_labels, cfg) is None  # strict: maybe labels haven't rendered yet
    relaxed = pick_video(no_labels, cfg, need_duration=False)  # last resort after retries
    assert relaxed is not None and relaxed.index == 0
    assert pick_video([cand(0, duration="1:02:03")], cfg) is not None  # hours format


def test_pick_none_when_nothing_playable(cfg: YouTubeConfig) -> None:
    assert pick_video([cand(0, ad=True), cand(1, href="/shorts/a")], cfg) is None


def test_search_url_is_encoded(cfg: YouTubeConfig) -> None:
    assert cfg.search("lofi & chill").endswith("search_query=lofi+%26+chill")


@pytest.mark.parametrize(
    ("url", "level"),
    [
        ("https://www.amazon.in/gp/buy/spc/handlers/display.html", RiskLevel.PAYMENT),
        ("https://www.flipkart.com/checkout/init", RiskLevel.PAYMENT),
        ("https://api.razorpay.com/v1/checkout/embedded", RiskLevel.PAYMENT),
        ("https://checkout.stripe.com/c/pay/cs_live", RiskLevel.PAYMENT),
        ("https://secure.paytm.com/pay", RiskLevel.PAYMENT),
    ],
)
def test_url_risk_payment(url: str, level: RiskLevel) -> None:
    rules = SafetyPolicy.from_yaml().rules.url_rules
    match = url_risk(url, rules)
    assert match is not None and match[0] == level


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abc",
        "https://www.amazon.in/s?k=earbuds",
        "https://stripe.com/docs",  # only checkout.stripe.com is a gateway
        "https://notrazorpay.com/",  # host must match exactly or as a subdomain
        "",
    ],
)
def test_url_risk_none(url: str) -> None:
    assert url_risk(url, SafetyPolicy.from_yaml().rules.url_rules) is None


def test_policy_uses_page_url_and_url_targets() -> None:
    policy = SafetyPolicy.from_yaml()
    pause = Action(kind="media_key", target="pause", page_url="https://www.amazon.in/checkout/p")
    assert policy.classify(pause)[0] == RiskLevel.PAYMENT
    opening = Action(kind="open_url", target="https://pages.razorpay.com/pl_x")
    assert policy.classify(opening)[0] == RiskLevel.PAYMENT
    # a launch_app target is a name, not a URL — never URL-checked
    assert policy.classify(Action(kind="launch_app", target="checkout"))[0] == RiskLevel.GREEN


def test_browser_kinds_are_green() -> None:
    policy = SafetyPolicy.from_yaml()
    for kind in ["youtube_play", "media_key", "browser_nav", "set_ad_policy", "open_url"]:
        assert policy.classify(Action(kind=kind, target="x"))[0] == RiskLevel.GREEN


class Named:
    def __init__(self, name: str) -> None:
        self.name = name

    async def execute(self, action: Action) -> ActionResult:
        return ActionResult(ok=True, observation=self.name)


async def test_kind_router() -> None:
    router = KindRouter(Named("os"), [({"open_url", "media_key"}, Named("browser"))])
    assert (await router.execute(Action(kind="open_url"))).observation == "browser"
    assert (await router.execute(Action(kind="set_volume"))).observation == "os"


def test_chrome_args_use_separate_data_folder(tmp_path: Path) -> None:
    args = chrome_args(Path("chrome.exe"), tmp_path, 9222, ["https://x.com"])
    assert f"--user-data-dir={tmp_path}" in args
    assert "--remote-debugging-port=9222" in args
    assert args[-1] == "https://x.com"
    assert not any("debugging" in a for a in chrome_args(Path("c.exe"), tmp_path, None))  # setup


async def test_session_without_chrome_raises(tmp_path: Path) -> None:
    settings = BrowserSettings(chrome_path=str(tmp_path / "missing.exe"),
                               profile_dir=str(tmp_path), cdp_port=9333)  # fmt: skip
    with pytest.raises(BrowserUnavailable, match="isn't installed"):
        await BrowserSession(settings).connect()
    with pytest.raises(BrowserUnavailable, match="isn't running"):
        await BrowserSession(settings).connect(launch=False)


@pytest.mark.parametrize(
    ("url", "found"),
    [
        ("https://www.youtube.com/oops", "account error"),
        ("https://accounts.google.com/ServiceLogin?continue=https://www.youtube.com", "sign in"),
        ("https://consent.youtube.com/m?continue=x", "cookie consent"),
        ("https://www.youtube.com/results?search_query=oops", None),  # a search for "oops" is fine
        ("https://www.youtube.com/watch?v=abc", None),
    ],
)
def test_youtube_error_pages(cfg: YouTubeConfig, url: str, found: str | None) -> None:
    message = cfg.check_error_page(url)
    assert (message is None) if found is None else (message is not None and found in message)
