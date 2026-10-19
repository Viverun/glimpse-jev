"""The visible pointer: smooth glides to each target, and glimpse's own grey cursor while it works.

glide(x, y) moves the real pointer (ydotool, GNOME logical pixels) along a slightly curved, eased path, so you
can follow what glimpse is about to click. The pointer position is not readable on Wayland: the last position
glimpse left it at is remembered in ~/.cache/jev/pointer.json.

The cursor is a small Xcursor theme ("glimpse-cursor", a grey arrow; every other shape is inherited from your
own theme). glow.py switches to it while a task runs and switches back afterwards, also if glimpse crashes.
"""

import json
import math
import os
import random
import struct
import subprocess
import time
from pathlib import Path

ENV = {**os.environ, "YDOTOOL_SOCKET": f"/run/user/{os.getuid()}/.ydotool_socket"}
STATE = Path.home() / ".cache" / "jev" / "pointer.json"
THEME = "glimpse-cursor"
THEME_DIR = Path.home() / ".local" / "share" / "icons" / THEME


def _move(x: float, y: float) -> None:
    subprocess.run(["ydotool", "mousemove", "--absolute", "-x", str(round(x)), "-y", str(round(y))],
                   env=ENV, capture_output=True)


def _last() -> tuple[float, float] | None:
    try:
        p = json.loads(STATE.read_text())
        return p["x"], p["y"]
    except Exception:
        return None


def glide(x: float, y: float, speed: float = 2600.0) -> None:
    """Move the pointer to (x, y) smoothly: ease-in-out, a gentle arc, 0.12-0.45 s depending on the distance."""
    start = _last()
    if os.environ.get("GLIMPSE_NO_GLIDE") or start is None:
        _move(x, y)
    else:
        x0, y0 = start
        dist = math.hypot(x - x0, y - y0)
        if dist > 3:
            dur = min(0.45, max(0.12, dist / speed)) * random.uniform(0.9, 1.15)  # never exactly the same twice
            steps = max(6, int(dur * 75))
            # control point a little to the side of the straight line, to a random side: a hand moves in arcs
            nx, ny = -(y - y0) / dist, (x - x0) / dist
            bend = min(60.0, dist * 0.08) * random.uniform(0.4, 1.0) * random.choice((-1, 1))
            cx, cy = (x0 + x) / 2 + nx * bend, (y0 + y) / 2 + ny * bend
            t0 = time.perf_counter()
            for i in range(1, steps + 1):
                t = i / steps
                e = 4 * t ** 3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2  # ease-in-out cubic
                px = (1 - e) ** 2 * x0 + 2 * (1 - e) * e * cx + e ** 2 * x
                py = (1 - e) ** 2 * y0 + 2 * (1 - e) * e * cy + e ** 2 * y
                _move(px, py)
                wait = t0 + dur * t - time.perf_counter()
                if wait > 0:
                    time.sleep(wait)
        _move(x, y)
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"x": x, "y": y}))
    except OSError:
        pass


BUTTONS = {"left": "0xC0", "right": "0xC1"}


def click(x: float, y: float, button: str = "left", count: int = 1) -> None:
    """Glide to (x, y), pause briefly like a hand settling on the target, and click with the real pointer
    (ydotool: a real input device, so every app accepts it)."""
    glide(x, y)
    time.sleep(random.uniform(0.05, 0.09))
    for i in range(count):
        subprocess.run(["ydotool", "click", BUTTONS[button]], env=ENV, capture_output=True)
        if i + 1 < count:
            time.sleep(0.06)


# --- glimpse's cursor theme ----------------------------------------------------------------------
def _arrow(size: int):
    """A soft grey arrow with a dark rim and a small ◐ dot in its tail, at `size` pixels."""
    from PIL import Image, ImageDraw
    s = size * 4  # draw large, scale down: smooth edges
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pts = [(0.10, 0.06), (0.10, 0.80), (0.29, 0.62), (0.42, 0.92), (0.54, 0.87), (0.41, 0.58), (0.66, 0.58)]
    pts = [(x * s, y * s) for x, y in pts]
    shadow = [(x + s * 0.025, y + s * 0.035) for x, y in pts]
    d.polygon(shadow, fill=(0, 0, 0, 70))
    d.polygon(pts, fill=(212, 215, 221, 255), outline=(38, 40, 45, 255), width=max(2, s // 36))
    r, cx, cy = s * 0.075, s * 0.22, s * 0.47
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(92, 97, 106, 255))
    d.pieslice((cx - r, cy - r, cx + r, cy + r), 90, 270, fill=(38, 40, 45, 255))
    return img.resize((size, size), Image.LANCZOS)


def _xcursor(sizes: list[int]) -> bytes:
    """Xcursor file with one image per size (hotspot at the arrow tip)."""
    imgs = []
    for n in sizes:
        im = _arrow(n)
        raw = im.tobytes()  # RGBA
        px = b"".join(struct.pack("<I", (a << 24) | (r * a // 255 << 16) | (g * a // 255 << 8) | (b * a // 255))
                      for r, g, b, a in zip(raw[0::4], raw[1::4], raw[2::4], raw[3::4]))  # premultiplied ARGB
        imgs.append((n, struct.pack("<9I", 36, 0xFFFD0002, n, 1, n, n, round(n * 0.10), round(n * 0.06), 0) + px))
    head = 16 + 12 * len(imgs)
    out, toc, pos = b"", b"", head
    for n, chunk in imgs:
        toc += struct.pack("<3I", 0xFFFD0002, n, pos)
        out += chunk
        pos += len(chunk)
    return b"Xcur" + struct.pack("<3I", 16, 0x10000, len(imgs)) + toc + out


def install_theme(inherits: str) -> str:
    """Write the theme (once) and return its name. Arrow shapes only; the rest comes from `inherits`."""
    if inherits == THEME:  # left over from a crash: never inherit from ourselves
        inherits = "Adwaita"
    cur = THEME_DIR / "cursors"
    if not (cur / "default").exists():
        cur.mkdir(parents=True, exist_ok=True)
        data = _xcursor([24, 32, 48, 64, 96])
        for name in ("default", "left_ptr", "arrow", "top_left_arrow"):
            (cur / name).write_bytes(data)
    (THEME_DIR / "index.theme").write_text(f"[Icon Theme]\nName=glimpse\nComment=glimpse's cursor while it works\nInherits={inherits}\n")
    return THEME


def current_theme() -> str:
    out = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "cursor-theme"], capture_output=True, text=True).stdout
    return out.strip().strip("'") or "Adwaita"


def set_theme(name: str) -> None:
    subprocess.run(["gsettings", "set", "org.gnome.desktop.interface", "cursor-theme", name], capture_output=True)


if __name__ == "__main__":  # preview: switch for 4 s and glide around
    old = current_theme()
    set_theme(install_theme(old))
    try:
        for x, y in ((300, 200), (1100, 600), (720, 405)):
            glide(x, y)
            time.sleep(0.6)
        time.sleep(2)
    finally:
        set_theme(old)
