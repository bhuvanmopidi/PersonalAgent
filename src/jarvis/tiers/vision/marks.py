"""Numbered boxes on a screenshot ("set-of-marks"): the vision model answers "box 7" instead of
guessing pixel positions, and Jarvis clicks the centre of box 7."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

from jarvis.tiers.vision.detector import Box

if TYPE_CHECKING:
    from PIL import Image

MAX_SIDE = 1600  # the model sees at most this many pixels on the long side (cost / speed)
JPEG_QUALITY = 80
_COLOURS = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#008080", "#9a6324", "#800000"]


def draw_marks(image: Image.Image, boxes: list[Box]) -> bytes:
    """The screenshot with each box outlined and numbered 1…n, as JPEG bytes (never saved)."""
    from PIL import ImageDraw, ImageFont

    picture = image.convert("RGB")
    scale = min(1.0, MAX_SIDE / max(picture.size))
    if scale < 1.0:
        picture = picture.resize((int(picture.width * scale), int(picture.height * scale)))
    draw = ImageDraw.Draw(picture)
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont
    try:
        font = ImageFont.truetype("arialbd.ttf", 14)
    except OSError:
        font = ImageFont.load_default()
    for n, box in enumerate(boxes, 1):
        colour = _COLOURS[n % len(_COLOURS)]
        x1, y1, x2, y2 = (v * scale for v in (box.x1, box.y1, box.x2, box.y2))
        draw.rectangle((x1, y1, x2, y2), outline=colour, width=2)
        label = str(n)
        tw, th = draw.textbbox((0, 0), label, font=font)[2:]
        tx, ty = x1, max(0.0, y1 - th - 2)
        draw.rectangle((tx, ty, tx + tw + 4, ty + th + 2), fill=colour)
        draw.text((tx + 2, ty), label, fill="white", font=font)
    buffer = io.BytesIO()
    picture.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    return buffer.getvalue()
