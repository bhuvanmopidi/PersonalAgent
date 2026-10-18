from pathlib import Path

import pytest

from jarvis.tiers.instant.app_index import AppEntry, AppIndex
from jarvis.tiers.instant.apps import (
    AliasConfig,
    AppAlias,
    AppLauncher,
    LaunchPlan,
    Prefer,
    load_aliases,
    parse_prefer,
)

ENTRIES = [
    AppEntry(name="Spotify", app_id="SpotifyAB.Spotify!Spotify"),
    AppEntry(name="Google Chrome", app_id="Chrome"),
    AppEntry(name="Visual Studio Code", app_id="Microsoft.VisualStudioCode"),
    AppEntry(name="Notepad", app_id="Microsoft.WindowsNotepad!App"),
    AppEntry(name="Notepad++", app_id="Notepad++"),
]


@pytest.fixture
def index() -> AppIndex:
    return AppIndex(ENTRIES)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("notepad", "Notepad"),  # exact beats Notepad++
        ("NOTEPAD", "Notepad"),
        ("note", "Notepad"),  # shortest prefix
        ("google", "Google Chrome"),
        ("chrome", "Google Chrome"),  # fuzzy partial
        ("spotfy", "Spotify"),  # typo
        ("visual studio", "Visual Studio Code"),
    ],
)
def test_index_find(index: AppIndex, query: str, expected: str) -> None:
    entry = index.find(query)
    assert entry is not None and entry.name == expected


@pytest.mark.parametrize("query", ["zzzz", "", "photoshop"])
def test_index_find_none(index: AppIndex, query: str) -> None:
    assert index.find(query) is None


def test_index_cache_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "apps.json"
    path.write_bytes(b'[{"name": "Spotify", "app_id": "X"}]')
    assert AppIndex.load(path).find("spotify") == AppEntry(name="Spotify", app_id="X")


class Loader:
    """Fake AppIndex.load that records refreshes."""

    def __init__(self, index: AppIndex) -> None:
        self.index, self.refreshes = index, 0

    def __call__(self, refresh: bool = False) -> AppIndex:
        self.refreshes += refresh
        return self.index


@pytest.fixture
def launcher(index: AppIndex) -> AppLauncher:
    aliases = AliasConfig(
        apps={
            "vs code": AppAlias(name="Visual Studio Code", process="Code"),
            "settings": AppAlias(uri="ms-settings:"),
        },
        sites={"youtube": "https://www.youtube.com"},
    )
    return AppLauncher(aliases, Loader(index), opener=lambda _: None)


@pytest.mark.parametrize(
    ("query", "kind", "value"),
    [
        ("spotify", "app", "SpotifyAB.Spotify!Spotify"),
        ("vs code", "app", "Microsoft.VisualStudioCode"),  # alias → index name
        ("settings", "uri", "ms-settings:"),
        ("youtube", "url", "https://www.youtube.com"),
        ("github.com", "url", "https://github.com"),
        ("https://example.org/x", "url", "https://example.org/x"),
    ],
)
def test_resolve(launcher: AppLauncher, query: str, kind: str, value: str) -> None:
    plan = launcher.resolve(query)
    assert plan is not None and (plan.kind, plan.value) == (kind, value)


@pytest.fixture
def web_launcher() -> AppLauncher:
    """Installed: WhatsApp, GitHub Desktop, Google Chrome. Sites: whatsapp, instagram, github…"""
    index = AppIndex(
        [
            AppEntry(name="WhatsApp", app_id="WhatsApp!App"),
            AppEntry(name="GitHub Desktop", app_id="GitHubDesktop"),
            AppEntry(name="Google Chrome", app_id="Chrome"),
        ]
    )
    aliases = AliasConfig(
        apps={"google chrome": AppAlias(name="Google Chrome", process="chrome")},
        sites={
            "whatsapp": "https://web.whatsapp.com",
            "instagram": "https://www.instagram.com",
            "github": "https://github.com",
            "google": "https://www.google.com",
            "x": "https://x.com",
        },
        prefer_web=["github", "google"],
    )
    return AppLauncher(aliases, Loader(index), opener=lambda _: None)


@pytest.mark.parametrize(
    ("query", "prefer", "value"),
    [
        ("whatsapp", "auto", "WhatsApp!App"),  # installed app beats the site
        ("whatsapp", "web", "https://web.whatsapp.com"),  # "open whatsapp website"
        ("whatsapp", "app", "WhatsApp!App"),
        ("instagram", "auto", "https://www.instagram.com"),  # not installed → site
        ("insta", "auto", "https://www.instagram.com"),  # prefix site match
        ("instagarm", "auto", "https://www.instagram.com"),  # typo
        ("github", "auto", "https://github.com"),  # prefer_web beats "GitHub Desktop"
        ("github", "app", "GitHubDesktop"),  # …unless the user says "app"
        ("google chrome", "auto", "Chrome"),  # not the "google" site
        ("chrome", "auto", "Chrome"),
        ("instagram.com", "auto", "https://instagram.com"),  # typed domain
    ],
)
def test_resolve_app_vs_website(
    web_launcher: AppLauncher, query: str, prefer: Prefer, value: str
) -> None:
    plan = web_launcher.resolve(query, prefer)
    assert plan is not None and plan.value == value


@pytest.mark.parametrize(
    ("query", "prefer"),
    [
        ("instagram", "app"),  # not installed, and the user asked for the app
        ("xbox", "auto"),  # must not fuzzy-match the site "x"
        ("photoshop", "web"),
    ],
)
def test_resolve_app_vs_website_not_found(
    web_launcher: AppLauncher, query: str, prefer: Prefer
) -> None:
    assert web_launcher.resolve(query, prefer) is None


@pytest.mark.parametrize(
    ("word", "prefer"),
    [("app", "app"), ("website", "web"), ("site", "web"), ("auto", "auto"), ("", "auto")],
)
def test_parse_prefer(word: str, prefer: Prefer) -> None:
    assert parse_prefer(word) == prefer


def test_resolve_unknown_refreshes_index_once(index: AppIndex) -> None:
    loader = Loader(index)
    launcher = AppLauncher(AliasConfig(), loader, opener=lambda _: None)
    assert launcher.resolve("definitely not an app") is None
    assert launcher.resolve("another unknown") is None  # within cooldown: no second rebuild
    assert loader.refreshes == 1


def test_launch_app_uses_apps_folder(index: AppIndex) -> None:
    opened: list[str] = []
    launcher = AppLauncher(AliasConfig(), Loader(index), opener=opened.append)
    launcher.launch(LaunchPlan(kind="app", value="Chrome", display="Google Chrome"))
    launcher.launch(LaunchPlan(kind="url", value="https://x.com", display="x"))
    assert opened == ["shell:AppsFolder\\Chrome", "https://x.com"]


@pytest.mark.parametrize("name", ["explorer", "svchost", "winlogon"])
def test_close_refuses_protected(launcher: AppLauncher, name: str) -> None:
    with pytest.raises(PermissionError):
        launcher.close(name)


def test_close_refuses_own_process(launcher: AppLauncher) -> None:
    with pytest.raises(PermissionError):
        launcher.close("python")  # pytest itself runs in python.exe


def test_real_aliases_file_loads() -> None:
    aliases = load_aliases()
    assert aliases.apps["chrome"].process == "chrome"
    assert aliases.sites["amazon"].startswith("https://")
