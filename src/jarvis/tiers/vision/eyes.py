"""The vision tier's eyes (Phase 15): when the text view can't show what's needed, look at the
screen — a screenshot of where the agent works, OmniParser's boxes on it, numbered.

Used by the agent loop (tiers/structured/agent.py) only when the agent asks to `look`, or when an
app window shows nothing readable. Screenshots stay in memory; they are never saved.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from jarvis.tiers.vision.detector import Box, Detector
from jarvis.tiers.vision.marks import draw_marks

if TYPE_CHECKING:
    from jarvis.tiers.structured.surface import Shot, Surface


class BoxFinder(Protocol):
    def detect(self, image: object) -> list[Box]: ...


@dataclass
class Look:
    shot: Shot
    boxes: list[Box]
    picture: bytes  # the screenshot with numbered boxes (JPEG), for the vision model

    def click_point(self, number: int) -> tuple[float, float] | None:
        """Where to click for box `number` (1-based): its centre, in click coordinates."""
        if not 1 <= number <= len(self.boxes):
            return None
        return self.shot.to_click(*self.boxes[number - 1].centre)


class Eyes:
    def __init__(self, finder: BoxFinder | None = None) -> None:
        self.finder = finder or Detector()

    async def look(self, surface: Surface) -> Look | None:
        shot = await surface.screenshot()
        if shot is None:
            return None
        boxes = await asyncio.to_thread(self.finder.detect, shot.image)
        picture = await asyncio.to_thread(draw_marks, shot.image, boxes)
        return Look(shot, boxes, picture)

    def warm(self) -> None:
        warm = getattr(self.finder, "warm", None)
        if warm is not None:
            warm()
