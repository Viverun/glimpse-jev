"""Fast window screenshots from the frame daemon (framed.py): a crop of the newest screencast frame, scaled and
encoded in a few milliseconds, with no portal call and no flash.

Used only when screen sharing was already approved once (the screencast restore token exists), so it never opens a
dialog by itself; otherwise, or when a window is not on the streamed monitor, glimpse falls back to the portal.
GLIMPSE_NO_FRAMES=1 or "fast_frames": false in config.json turns it off.
"""

import io
import mmap
import os
import struct
import subprocess
import time
from pathlib import Path

HEADER = struct.Struct("<4sIQdIIIIiiI")  # ... monitor x, y, daemon pid
SIZE = 64
MAX_W, MAX_H = 3840, 2400
SLOT = MAX_W * MAX_H * 3
TOKEN = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "glimpse" / "screencast.token"


def enabled() -> bool:
    if os.environ.get("GLIMPSE_NO_FRAMES") or not TOKEN.exists() or not os.path.exists("/usr/bin/python3"):
        return False
    try:
        import config
        return config.get("fast_frames", True) is not False
    except Exception:
        return True


TOAST_AREA = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "glimpse-toast-area")


def covered(bounds: dict) -> bool:
    """A glimpse notification is showing over part of this window (the stream would include it): use the portal."""
    try:
        x, y, w, h, until = (float(v) for v in open(TOAST_AREA).read().split())
    except (OSError, ValueError):
        return False
    if time.time() > until:
        return False
    return not (x >= bounds["x"] + bounds["width"] or x + w <= bounds["x"]
                or y >= bounds["y"] + bounds["height"] or y + h <= bounds["y"])


class Frames:
    def __init__(self):
        self.proc = None
        self.mem = None
        self.failed = False

    def start(self) -> bool:
        """Start the daemon (once) and wait for its first frame (~1 s). False if it cannot run here."""
        if self.mem is not None:
            return True
        if self.failed or not enabled():
            return False
        shm = f"/dev/shm/glimpse-frames-{os.getuid()}"
        if not self._attach(shm):
            self.proc = subprocess.Popen(["/usr/bin/python3", str(Path(__file__).parent / "framed.py"), str(os.getpid())],
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         start_new_session=True)
            t0 = time.monotonic()
            while time.monotonic() - t0 < 8 and not self._attach(shm):
                if self.proc.poll() is not None:
                    break
                time.sleep(0.05)
        if self.mem is None:
            self.failed = True
            self.stop()
        return self.mem is not None

    def _attach(self, shm: str) -> bool:
        try:
            fd = os.open(shm, os.O_RDONLY)
        except OSError:
            return False
        try:
            if os.fstat(fd).st_size < SIZE + 2 * SLOT:
                return False
            mem = mmap.mmap(fd, SIZE + 2 * SLOT, prot=mmap.PROT_READ)
        finally:
            os.close(fd)
        h = HEADER.unpack(mem[:HEADER.size])
        alive = False
        try:
            os.kill(h[10], 0)
            alive = h[0] == b"GLFR" and h[2] > 0
        except (ProcessLookupError, PermissionError, OverflowError):
            pass
        if not alive:  # a file left by a daemon that died: its frames would never change again
            mem.close()
            return False
        self.mem = mem
        return True

    def header(self):
        magic, slot, seq, t, w, h, lw, lh, mx, my, pid = HEADER.unpack(self.mem[:HEADER.size])
        return {"slot": slot, "seq": seq, "time": t, "w": w, "h": h, "lw": lw, "lh": lh, "mx": mx, "my": my}

    def frame(self, after: float = 0.0, quiet: float = 0.06, wait: float = 0.3):
        """(RGB numpy array of the whole monitor, header), accurate after an action: when something was done since
        the last frame (`after`), wait for the screen to draw it, i.e. until no new frame came for `quiet` s, at most
        `wait` s. Frames come only when the screen changes, so a quiet stream is the current screen."""
        import numpy as np
        t0 = time.monotonic()
        if after and time.time() - after < 1.0:
            seq, since = self.header()["seq"], time.monotonic()
            while time.monotonic() - t0 < wait:
                time.sleep(0.008)
                h = self.header()
                if h["seq"] != seq:
                    seq, since = h["seq"], time.monotonic()
                elif time.monotonic() - since >= quiet and (h["time"] >= after or time.monotonic() - t0 >= 0.12):
                    break
        for _ in range(3):
            h = self.header()
            off = SIZE + h["slot"] * SLOT
            arr = np.frombuffer(self.mem, np.uint8, h["w"] * h["h"] * 3, off).reshape(h["h"], h["w"], 3).copy()
            if self.header()["seq"] - h["seq"] < 2:  # the writer did not come back round to this slot meanwhile
                return arr, h
        return arr, h

    def window_shot(self, bounds: dict, after: float = 0.0, fmt: str = "jpeg", quality: int = 70,
                    max_width: int = 960) -> tuple[bytes, dict] | None:
        """A window's screenshot like computer-use-linux returns it: (image bytes, screenshot metadata). None when
        the window is not fully on the streamed monitor."""
        from PIL import Image
        arr, h = self.frame(after)
        sx, sy = h["w"] / h["lw"], h["h"] / h["lh"]  # logical -> physical pixels (GNOME scaling)
        x0, y0 = round((bounds["x"] - h["mx"]) * sx), round((bounds["y"] - h["my"]) * sy)
        x1, y1 = round(x0 + bounds["width"] * sx), round(y0 + bounds["height"] * sy)
        if x0 < 0 or y0 < 0 or x1 > h["w"] + 2 or y1 > h["h"] + 2:
            return None  # not entirely on the streamed monitor
        x1, y1 = min(h["w"], x1), min(h["h"], y1)
        if x1 - x0 < 8 or y1 - y0 < 8 or covered(bounds):
            return None
        img = Image.fromarray(arr[y0:y1, x0:x1])
        cw, ch = img.size
        if cw > max_width:
            img = img.resize((max_width, max(1, round(ch * max_width / cw))), Image.BILINEAR)
        buf = io.BytesIO()
        if fmt == "png":
            img.save(buf, "PNG", compress_level=1)
        else:
            img.save(buf, "JPEG", quality=quality)
        meta = {"width": img.width, "height": img.height, "coordinate_width": cw, "coordinate_height": ch,
                "scale": img.width / cw, "format": fmt, "source": "screencast", "cropped_to_window": True}
        return buf.getvalue(), meta

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.proc = None


_frames: Frames | None = None
_lock = __import__("threading").Lock()
_last_action = 0.0


def source() -> Frames | None:
    """The shared frame source, started on first use; None when fast frames are not available."""
    global _frames
    with _lock:
        if _frames is None:
            _frames = Frames()
        return _frames if _frames.start() else None


def mark_action() -> None:
    """Something was just done to the screen: the next screenshot waits for a frame from after it."""
    global _last_action
    _last_action = time.time()


def last_action() -> float:
    return _last_action
