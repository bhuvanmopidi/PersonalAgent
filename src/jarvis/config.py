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
    # hybrid: rules, then the LLM for what they miss · llm_first: the reverse (an experiment)
    mode: Literal["hybrid", "llm_first"] = "hybrid"
    embedding_match_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    classifier_threshold: float = Field(default=0.8, ge=0.0, le=1.0)


class LLMSettings(BaseModel):
    enabled: bool = True
    # online: which model understands what the rules miss. Offline it's always the local model.
    online_provider: Literal["openrouter", "openai", "claude", "local"] = "openrouter"
    # OpenRouter (needs OPENROUTER_API_KEY): Claude Haiku 5.5, chosen by `jarvis eval-router`
    openrouter_model: str = "anthropic/claude-haiku-5.5"
    openai_model: str = "gpt-4o-mini"  # OpenAI model for understanding commands
    router_model: str = "claude-haiku-5-5"  # Claude model for understanding commands
    local_model: str = "qwen2.5:3b"  # Ollama model (offline, or online when chosen)
    ollama_url: str = "http://127.0.0.1:11434"
    effort: Literal["low", "medium", "high"] = "low"
    timeout_s: float = Field(default=8.0, gt=0)
    model: str = "claude-opus-5-5"  # planner / agent model for later phases


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


class BrowserSettings(BaseModel):
    enabled: bool = True  # false → links open in the default browser, no automation
    chrome_path: str = ""  # "" = find chrome.exe automatically
    profile_dir: str = ""  # "" = %LOCALAPPDATA%\JarvisAgent\chrome-profile
    cdp_port: int = Field(default=9222, ge=1024, le=65535)
    launch_timeout_s: float = Field(default=15.0, gt=0)


class Settings(BaseModel):
    log_level: str = "INFO"
    hotkeys: HotkeySettings = HotkeySettings()
    router: RouterSettings = RouterSettings()
    llm: LLMSettings = LLMSettings()
    tts: TTSSettings = TTSSettings()
    listen: ListenSettings = ListenSettings()
    stt: STTSettings = STTSettings()
    browser: BrowserSettings = BrowserSettings()


def load_settings(path: Path = DEFAULT_SETTINGS_PATH) -> Settings:
    """Read settings YAML; missing file or keys fall back to defaults."""
    if not path.exists():
        return Settings()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Settings.model_validate(data)
