"""Skills (Tier 2): recipes learned from tasks that worked, replayed without asking the AI
each step.

A skill is a short list of steps with slots ({song}, {to}, …). Controls are kept by what they are
(type + name), never by number or position. Stored locally as JSON, one file per skill, in
%LOCALAPPDATA%\\JarvisAgent\\skills (Phase 13 moves them into the encrypted database).
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from jarvis.paths import data_dir
from jarvis.telemetry.logging import get_logger

log = get_logger(__name__)

RETIRE_AFTER = 3  # failed replays in a row → dropped (learned fresh next time)
_SLOT = re.compile(r"\{(\w+)\}")


class SkillStep(BaseModel):
    op: str  # an agent op: click, type, press, select, navigate, open_app, …
    tag: str = ""  # the control's type ("button", "combo box") — empty for steps without one
    name: str = ""  # the control's name, with slots ("Play {song}")
    text: str = ""  # typed / selected text, with slots
    key: str = ""
    url: str = ""  # navigate / new tab address, with slots


class Skill(BaseModel):
    id: str
    name: str  # "play {song} on Spotify"
    description: str = ""
    where: Literal["browser", "desktop"] = "browser"
    slots: dict[str, str] = Field(default_factory=dict)  # slot → what it is ("the song")
    steps: list[SkillStep] = Field(default_factory=list)
    check: str = ""  # what the screen shows at the end, with slots ("{song}")
    done_message: str = ""  # what to say, with slots
    successes: int = 0
    failures: int = 0
    fails_in_row: int = 0
    created: float = Field(default_factory=time.time)
    last_used: float = 0.0

    def slot_names(self) -> set[str]:
        names = set(self.slots)
        for text in (self.name, self.check, self.done_message):
            names |= set(_SLOT.findall(text))
        return names


def fill(template: str, values: dict[str, str]) -> str:
    """'Play {song}' + {song: 'Shape of You'} → 'Play Shape of You' (unknown slots stay)."""
    return _SLOT.sub(lambda m: values.get(m.group(1), m.group(0)), template)


def spoken(name: str) -> str:
    """'play {song} on Spotify' → 'play song on Spotify' (for saying a skill's name)."""
    return _SLOT.sub(lambda m: m.group(1), name)


def slug(name: str) -> str:
    text = _SLOT.sub(lambda m: m.group(1), name.lower())
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")[:60] or "skill"


def default_skills_dir() -> Path:
    return data_dir() / "skills"


class SkillStore:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or default_skills_dir()
        self._skills: dict[str, Skill] | None = None

    def _load(self) -> dict[str, Skill]:
        if self._skills is None:
            self._skills = {}
            if self.directory.exists():
                for path in sorted(self.directory.glob("*.json")):
                    try:
                        skill = Skill.model_validate_json(path.read_text(encoding="utf-8"))
                        self._skills[skill.id] = skill
                    except (OSError, ValueError):
                        log.warning("skipping unreadable skill file %s", path)
        return self._skills

    def all(self) -> list[Skill]:
        return sorted(self._load().values(), key=lambda s: s.name.lower())

    def get(self, skill_id: str) -> Skill | None:
        return self._load().get(skill_id)

    def save(self, skill: Skill) -> None:
        """A new skill, or a better version of one with the same name (its record is kept)."""
        skills = self._load()
        old = skills.get(skill.id)
        if old is not None:
            kept = {"successes": old.successes, "failures": old.failures,
                    "created": old.created, "fails_in_row": 0}  # fmt: skip
            skill = skill.model_copy(update=kept)
        skills[skill.id] = skill
        self._write(skill)

    def record(self, skill_id: str, ok: bool) -> bool:
        """Count a replay; returns True if the skill was retired (too many failures in a row)."""
        skill = self._load().get(skill_id)
        if skill is None:
            return False
        skill.last_used = time.time()
        if ok:
            skill.successes += 1
            skill.fails_in_row = 0
        else:
            skill.failures += 1
            skill.fails_in_row += 1
            if skill.fails_in_row >= RETIRE_AFTER:
                log.info("retiring skill %s after %d failures in a row", skill.id, RETIRE_AFTER)
                self.remove(skill_id)
                return True
        self._write(skill)
        return False

    def remove(self, skill_id: str) -> bool:
        skill = self._load().pop(skill_id, None)
        if skill is None:
            return False
        (self.directory / f"{skill_id}.json").unlink(missing_ok=True)
        return True

    def find(self, words: str) -> list[Skill]:
        """Skills whose name / description mention these words ("the spotify skill")."""
        wanted = [w for w in re.findall(r"\w+", words.lower()) if w not in ("the", "skill", "my")]
        return [
            s for s in self.all()
            if wanted and all(w in f"{s.name} {s.description}".lower() for w in wanted)
        ]  # fmt: skip

    def slots_of(self, skill_id: str) -> set[str] | None:
        skill = self.get(skill_id)
        return set(skill.slots) if skill else None

    def catalog(self) -> str:
        """For the router: the skills, like its list of commands (empty if none)."""
        skills = self.all()
        if not skills:
            return ""
        lines = [
            "Skills (tasks done before — Jarvis repeats them fast; use one when the request is "
            'the same kind of task, with kind "task", `skill` = its id and its slots):'
        ]
        for s in skills:
            slots = ", ".join(f"{k} ({v})" for k, v in s.slots.items()) or "none"
            lines.append(f"- {s.id}: {s.name} [{s.where}] — slots: {slots}")
        return "\n".join(lines)

    def _write(self, skill: Skill) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / f"{skill.id}.json"
            path.write_text(json.dumps(skill.model_dump(), indent=2, ensure_ascii=False),
                            encoding="utf-8")  # fmt: skip
        except OSError:
            log.warning("couldn't save skill %s", skill.id, exc_info=True)
