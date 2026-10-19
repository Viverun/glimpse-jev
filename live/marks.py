"""Set-of-marks grounding: find real UI elements in a crop and number them, so Jev picks one.

Box answers are a few percent off, enough to miss a small switch or checkbox. Picking among
element boxes found by edge detection snaps the click to the true center of a real element.
"""

import io

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

MIN_SIDE, MAX_SIDE = 8, 220  # element size range in crop pixels
MAX_MARKS = 30


def find_elements(jpeg: bytes) -> list[tuple[int, int, int, int]]:
    """Candidate element rectangles (x, y, w, h), nearest to the crop center first."""
    img = np.array(Image.open(io.BytesIO(jpeg)).convert("L"))
    edges = cv2.Canny(img, 30, 90)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    rects = [cv2.boundingRect(c) for c in contours]
    rects = [r for r in rects if MIN_SIDE <= r[2] <= MAX_SIDE and MIN_SIDE <= r[3] <= MAX_SIDE]
    h, w = img.shape
    rects.sort(key=lambda r: (r[0] + r[2] / 2 - w / 2) ** 2 + (r[1] + r[3] / 2 - h / 2) ** 2)
    kept: list[tuple[int, int, int, int]] = []
    for r in rects:  # drop near-duplicates (same element found twice)
        if all(_iou(r, k) < 0.6 for k in kept):
            kept.append(r)
        if len(kept) == MAX_MARKS:
            break
    return kept


def draw_marks(jpeg: bytes, rects: list[tuple[int, int, int, int]], scale: int = 2) -> bytes:
    """Upscaled crop with each rectangle outlined and numbered from 1."""
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    img = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    d = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=14)
    for i, (x, y, w, h) in enumerate(rects, 1):
        box = (x * scale, y * scale, (x + w) * scale, (y + h) * scale)
        d.rectangle(box, outline=(255, 0, 0), width=2)
        tx, ty = box[0], max(box[1] - 16, 0)
        d.rectangle((tx, ty, tx + 8 * len(str(i)) + 4, ty + 15), fill=(255, 0, 0))
        d.text((tx + 2, ty), str(i), fill=(255, 255, 255), font=font)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue()


def _iou(a, b) -> float:
    ax2, ay2, bx2, by2 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw = max(0, min(ax2, bx2) - max(a[0], b[0]))
    ih = max(0, min(ay2, by2) - max(a[1], b[1]))
    inter = iw * ih
    return inter / (a[2] * a[3] + b[2] * b[3] - inter or 1)
