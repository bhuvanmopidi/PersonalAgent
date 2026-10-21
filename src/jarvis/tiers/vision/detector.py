"""Find what can be clicked in a screenshot — OmniParser's detector (Phase 15).

Microsoft OmniParser v2's `icon_detect` model (a YOLO detector trained on screenshots) finds
interactive regions — buttons, icons, swatches, fields — in ANY picture: websites, apps, games.
It runs locally on the GPU (CPU fallback). The weights (~40 MB) are downloaded once from Hugging
Face into the Jarvis data folder.

Licence note: the detector weights are AGPL-3.0 — fine for personal use; a product would swap in
a differently licensed detector (only this module changes).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis.paths import data_dir
from jarvis.telemetry.logging import get_logger

if TYPE_CHECKING:
    from PIL import Image

log = get_logger(__name__)

REPO = "microsoft/OmniParser-v2.0"
WEIGHTS = "icon_detect/model.pt"
MIN_CONFIDENCE = 0.05  # OmniParser's own default: recall matters, the vision model judges
OVERLAP = 0.7  # boxes overlapping more than this are the same thing
MAX_BOXES = 120


@dataclass(frozen=True)
class Box:
    """A region in screenshot pixels: left, top, right, bottom."""

    x1: float
    y1: float
    x2: float
    y2: float
    confidence: float = 1.0

    @property
    def centre(self) -> tuple[float, float]:
        return (self.x1 + self.x2) / 2, (self.y1 + self.y2) / 2

    @property
    def area(self) -> float:
        return max(0.0, self.x2 - self.x1) * max(0.0, self.y2 - self.y1)


def overlap(a: Box, b: Box) -> float:
    """Intersection over the smaller box: 1.0 when one sits inside the other."""
    w = min(a.x2, b.x2) - max(a.x1, b.x1)
    h = min(a.y2, b.y2) - max(a.y1, b.y1)
    if w <= 0 or h <= 0:
        return 0.0
    return (w * h) / max(1.0, min(a.area, b.area))


def tidy_boxes(boxes: list[Box], limit: int = MAX_BOXES) -> list[Box]:
    """Drop near-duplicates (keep the more confident), order top-to-bottom, left-to-right like
    reading — so box numbers go the way a person scans the screen."""
    kept: list[Box] = []
    for box in sorted(boxes, key=lambda b: -b.confidence):
        if box.area < 16:
            continue
        if all(overlap(box, k) < OVERLAP for k in kept):
            kept.append(box)
        if len(kept) >= limit:
            break
    return sorted(kept, key=lambda b: (round(b.y1 / 12), b.x1))


def weights_path() -> Path:
    return data_dir() / "models" / "omniparser" / WEIGHTS


class Detector:
    """Loads the model on first use (or `warm()` at startup) and keeps it on the GPU."""

    def __init__(self) -> None:
        self._model: Any = None
        self._lock = threading.Lock()

    def _load(self) -> Any:
        with self._lock:
            if self._model is None:
                path = weights_path()
                if not path.exists():
                    from huggingface_hub import hf_hub_download

                    log.info("downloading the OmniParser detector (~40 MB, once)")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    hf_hub_download(REPO, WEIGHTS, local_dir=path.parent.parent)
                from ultralytics import YOLO  # type: ignore[attr-defined]

                model = YOLO(str(path))
                try:
                    import torch

                    device = "cuda" if torch.cuda.is_available() else "cpu"
                except ImportError:
                    device = "cpu"
                model.to(device)
                self._model = model
                log.info("OmniParser detector ready on %s", device)
            return self._model

    def warm(self) -> None:
        self._load()

    def detect(self, image: Image.Image) -> list[Box]:
        """Clickable regions in the picture (blocking — call from a worker thread)."""
        model = self._load()
        result = model.predict(image, conf=MIN_CONFIDENCE, iou=0.1, verbose=False)[0]
        boxes = [
            Box(float(x1), float(y1), float(x2), float(y2), float(c))
            for (x1, y1, x2, y2), c in zip(
                result.boxes.xyxy.tolist(), result.boxes.conf.tolist(), strict=True
            )
        ]
        return tidy_boxes(boxes)
