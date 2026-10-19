"""Text on a screenshot (RapidOCR, local and offline), for clicking things the accessibility tree does not show.

glimpse uses it only as a fallback, when a click target is not in the tree (web pages that publish nothing, canvases,
custom widgets): the text boxes become a short list Jev chooses from, which is more exact than drawing a free box.
About 1-2 s per screenshot on a laptop CPU, so results are cached per screenshot (repeated looks at the same screen
cost nothing). GLIMPSE_NO_OCR=1, or "ocr": false in config.json, turns it off.
"""

import hashlib
import io
import os
import re
from collections import OrderedDict

_engine = None
_cache: OrderedDict[str, list[tuple[str, tuple[float, float, float, float]]]] = OrderedDict()


def enabled() -> bool:
    if os.environ.get("GLIMPSE_NO_OCR"):
        return False
    try:
        import config
        if config.get("ocr") is False:
            return False
        import rapidocr  # noqa: F401
        return True
    except ImportError:
        return False


def _get_engine():
    global _engine
    if _engine is None:
        import logging

        from rapidocr import RapidOCR
        logging.getLogger("RapidOCR").setLevel(logging.WARNING)
        _engine = RapidOCR(params={"Global.use_cls": False})  # screen text is upright: skip the rotation pass
    return _engine


def texts(shot: bytes) -> list[tuple[str, tuple[float, float, float, float]]]:
    """(text, (left, top, right, bottom) as fractions 0-1 of the screenshot) for each line of text, cached."""
    key = hashlib.sha1(shot).hexdigest()
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    from PIL import Image
    img = Image.open(io.BytesIO(shot)).convert("RGB")
    r = _get_engine()(img, use_cls=False)
    out = []
    for box, txt, score in zip(r.boxes if r.boxes is not None else [], r.txts or [], r.scores or []):
        if float(score) < 0.6 or not str(txt).strip():
            continue
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        out.append((str(txt).strip(), (min(xs) / img.width, min(ys) / img.height, max(xs) / img.width, max(ys) / img.height)))
    _cache[key] = out
    while len(_cache) > 8:
        _cache.popitem(last=False)
    return out


def _words(t: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{2,}", t.lower()))


def candidates(target: str, found: list[tuple[str, tuple]], limit: int = 60) -> list[tuple[str, tuple]]:
    """The text boxes most likely to be `target` first (shared words), at most `limit` of them."""
    w = _words(target)
    ranked = sorted(found, key=lambda t: -len(w & _words(t[0])))
    return ranked[:limit]
