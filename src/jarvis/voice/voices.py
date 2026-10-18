"""Piper voice models: where they live and how to download them."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from urllib.request import urlopen

from jarvis.paths import data_dir


def voices_dir() -> Path:
    path = data_dir() / "voices"
    path.mkdir(parents=True, exist_ok=True)
    return path


def voice_path(name: str) -> Path:
    return voices_dir() / f"{name}.onnx"


def is_downloaded(name: str) -> bool:
    model = voice_path(name)
    return model.exists() and Path(f"{model}.json").exists()


def download(name: str) -> Path:
    """Download model + config from the official rhasspy/piper-voices repo.

    Files go to a temp folder first, so an interrupted download never leaves a broken voice.
    """
    from piper.download_voices import download_voice

    partial = voices_dir() / ".partial"
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir()
    try:
        download_voice(name, partial)
        for file in partial.iterdir():
            file.replace(voices_dir() / file.name)
    finally:
        shutil.rmtree(partial, ignore_errors=True)
    return voice_path(name)


def list_available() -> list[str]:
    from piper.download_voices import VOICES_JSON

    with urlopen(VOICES_JSON) as response:
        return sorted(json.load(response))


def list_downloaded() -> list[str]:
    return sorted(p.stem for p in voices_dir().glob("*.onnx"))
