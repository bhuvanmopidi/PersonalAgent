"""Load config/settings.yaml into typed Pydantic settings."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
DEFAULT_SETTINGS_PATH = CONFIG_DIR / "settings.yaml"


class HotkeySettings(BaseModel):
    kill: str = "esc"


class RouterSettings(BaseModel):
    embedding_match_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    classifier_threshold: float = Field(default=0.8, ge=0.0, le=1.0)


class LLMSettings(BaseModel):
    provider: str = "anthropic"
    model: str = "claude-opus-5-5"


class Settings(BaseModel):
    log_level: str = "INFO"
    hotkeys: HotkeySettings = HotkeySettings()
    router: RouterSettings = RouterSettings()
    llm: LLMSettings = LLMSettings()


def load_settings(path: Path = DEFAULT_SETTINGS_PATH) -> Settings:
    """Read settings YAML; missing file or keys fall back to defaults."""
    if not path.exists():
        return Settings()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Settings.model_validate(data)
