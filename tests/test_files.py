import os
import time
from pathlib import Path

from jarvis.tiers.instant.files import find_files, known_folder


def touch(path: Path, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_find_best_match_first(tmp_path: Path) -> None:
    touch(tmp_path / "notes.txt")
    best = touch(tmp_path / "docs" / "Bhuvan_Resume_2026.pdf")
    assert find_files("resume", [tmp_path])[0] == best


def test_newest_wins_on_tie(tmp_path: Path) -> None:
    now = time.time()
    touch(tmp_path / "a" / "resume.pdf", now - 1000)
    newest = touch(tmp_path / "b" / "resume.docx", now)
    assert find_files("resume", [tmp_path])[0] == newest


def test_skips_hidden_and_junk_dirs(tmp_path: Path) -> None:
    touch(tmp_path / ".git" / "resume.txt")
    touch(tmp_path / "node_modules" / "resume.js")
    assert find_files("resume", [tmp_path]) == []


def test_respects_depth(tmp_path: Path) -> None:
    deep = tmp_path / "1" / "2" / "3" / "resume.pdf"
    touch(deep)
    assert find_files("resume", [tmp_path], max_depth=2) == []
    assert find_files("resume", [tmp_path], max_depth=5) == [deep]


def test_no_match(tmp_path: Path) -> None:
    touch(tmp_path / "holiday.jpg")
    assert find_files("resume", [tmp_path]) == []


def test_known_folders() -> None:
    assert known_folder("home") == Path.home()
    downloads = known_folder("downloads")
    assert downloads is not None and downloads.exists()
    assert known_folder("not-a-folder") is None
