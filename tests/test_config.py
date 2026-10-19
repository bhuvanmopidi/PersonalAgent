from pathlib import Path

from jarvis.config import DEFAULT_SETTINGS_PATH, Settings, load_settings


def test_loads_repo_settings() -> None:
    settings = load_settings(DEFAULT_SETTINGS_PATH)
    assert settings.hotkeys.kill == "esc"
    assert settings.llm.router_model == "claude-haiku-5-5"
    assert settings.router.mode == "hybrid"


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    assert load_settings(tmp_path / "nope.yaml") == Settings()


def test_partial_file_fills_defaults(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text("log_level: DEBUG\n", encoding="utf-8")
    settings = load_settings(path)
    assert settings.log_level == "DEBUG"
    assert settings.router.embedding_match_threshold == 0.85
