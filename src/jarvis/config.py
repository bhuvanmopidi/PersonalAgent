"""Load config/settings.yaml into typed Pydantic settings."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
DEFAULT_SETTINGS_PATH = CONFIG_DIR / "settings.yaml"


class HotkeySettings(BaseModel):
    kill: str = "esc"
    push_to_talk: str = "<ctrl>+<alt>+j"  # pynput hotkey syntax; "" disables


class RouterSettings(BaseModel):
    embedding_match_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    classifier_threshold: float = Field(default=0.8, ge=0.0, le=1.0)


class LLMSettings(BaseModel):
    provider: str = "anthropic"
    model: str = "claude-opus-5-5"


class TTSSettings(BaseModel):
    engine: Literal["piper", "sapi", "none"] = "piper"
    voice: str = "en_US-lessac-medium"
    speed: float = Field(default=1.0, gt=0.25, le=3.0)


class ListenSettings(BaseModel):
    wake_word: str = "hey_jarvis"  # bundled openWakeWord model
    wake_threshold: float = Field(default=0.5, ge=0.05, le=0.99)
    vad_threshold: float = Field(default=0.5, ge=0.05, le=0.99)
    silence_ms: int = Field(default=700, ge=200, le=3000)  # pause that ends a command
    no_speech_timeout_s: float = Field(default=4.0, gt=0)  # woke up but nothing said
    max_utterance_s: float = Field(default=15.0, gt=0)
    wake_sound: bool = True  # short beep when the wake word is heard
    partials: bool = True  # live transcript while you speak (GPU only)


class STTSettings(BaseModel):
    model: str = "small.en"  # used on the GPU
    cpu_model: str = "base.en"  # used when CUDA isn't available
    device: Literal["auto", "cuda", "cpu"] = "auto"
    # extra words Whisper should favour (app/site names from aliases.yaml are added automatically)
    hotwords: list[str] = Field(default_factory=list)


class Settings(BaseModel):
    log_level: str = "INFO"
    hotkeys: HotkeySettings = HotkeySettings()
    router: RouterSettings = RouterSettings()
    llm: LLMSettings = LLMSettings()
    tts: TTSSettings = TTSSettings()
    listen: ListenSettings = ListenSettings()
    stt: STTSettings = STTSettings()


def load_settings(path: Path = DEFAULT_SETTINGS_PATH) -> Settings:
    """Read settings YAML; missing file or keys fall back to defaults."""
    if not path.exists():
        return Settings()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Settings.model_validate(data)
