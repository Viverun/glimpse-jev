"""Live desktop backend for the agent: GNOME Wayland screenshots via the XDG portal, input via ydotool.

Same interface as agent.loop.VM (shot, crop, act), so the agent runs unchanged.
Safety: only the action forms the agent generates are accepted, terminal/launcher shortcuts are
refused, and creating the file STOP_FILE aborts before the next action.
"""

import io
import os
import re
import subprocess
import time
from pathlib import Path

from PIL import Image

PORTAL_SHOT = Path(__file__).with_name("portal_shot.py")
STOP_FILE = Path.home() / "glimse-demo" / "STOP"
YDOTOOL_ENV = {**os.environ, "YDOTOOL_SOCKET": f"/run/user/{os.getuid()}/.ydotool_socket"}

KEYCODES = {
    "ctrl": 29, "control": 29, "shift": 42, "alt": 56, "super": 125, "win": 125,
    "enter": 28, "return": 28, "esc": 1, "escape": 1, "tab": 15, "backspace": 14, "delete": 111,
    "space": 57, "up": 103, "down": 108, "left": 105, "right": 106, "home": 102, "end": 107,
    "pageup": 104, "pagedown": 109,
    **{f"f{i}": 58 + i for i in range(1, 11)}, "f11": 87, "f12": 88,
    **{c: k for c, k in zip("1234567890", range(2, 12))},
    **dict(zip("qwertyuiop", range(16, 26))), **dict(zip("asdfghjkl", range(30, 39))),
    **dict(zip("zxcvbnm", range(44, 51))),
    "-": 12, "=": 13, ",": 51, ".": 52, "/": 53,
}
# Terminal and run-command shortcuts. Super (Activities) is allowed: it is how GNOME launches apps.
BLOCKED_COMBOS = [{"ctrl", "alt", "t"}, {"alt", "f2"}]


class Stopped(Exception):
    pass


class flat_pointer:
    """Context manager for agent runs; restores every setting afterwards.

    - flat pointer acceleration: ydotool absolute moves are only exact without acceleration
    - no animations: removes GNOME's flash on every portal screenshot
    - no event sounds: removes the screenshot shutter sound
    """

    SETTINGS = [
        ("org.gnome.desktop.peripherals.mouse", "accel-profile", "flat"),
        ("org.gnome.desktop.interface", "enable-animations", "false"),
        ("org.gnome.desktop.sound", "event-sounds", "false"),
    ]

    def __enter__(self):
        self.old = []
        for schema, key, value in self.SETTINGS:
            old = subprocess.run(["gsettings", "get", schema, key], capture_output=True, text=True).stdout.strip()
            self.old.append((schema, key, old))
            subprocess.run(["gsettings", "set", schema, key, value], check=True)
        return self

    def __exit__(self, *exc):
        for schema, key, old in self.old:
            subprocess.run(["gsettings", "set", schema, key, old.strip("'")], check=True)


class LiveVM:
    width, height = 1920, 1080  # screenshot space: the portal returns 1920x1080 images
    # ydotool moves in GNOME's logical pointer space; measured with a fullscreen GTK clickpad.
    # 2880x1620 panel at 2x scale -> 1440x810.
    pointer_w, pointer_h = 1440, 810

    def __init__(self, shot_w: int = 960, shot_h: int = 540):
        self.shot_w, self.shot_h = shot_w, shot_h

    # --- screenshots -------------------------------------------------------------------------
    def _full(self) -> Image.Image:
        path = subprocess.run(["/usr/bin/python3", str(PORTAL_SHOT)], capture_output=True, text=True, check=True).stdout.strip()
        try:
            img = Image.open(path).convert("RGB")
            img.load()
        finally:
            Path(path).unlink(missing_ok=True)  # the portal saves into ~/Pictures; don't leave it there
        if img.size != (self.width, self.height):
            img = img.resize((self.width, self.height), Image.LANCZOS)
        return img

    def shot(self) -> bytes:
        out = io.BytesIO()
        self._full().resize((self.shot_w, self.shot_h), Image.LANCZOS).save(out, "JPEG", quality=75)
        return out.getvalue()

    def crop(self, cx: int, cy: int, width: int, height: int) -> bytes:
        src = self._full()
        out = Image.new("RGB", (width, height), (128, 128, 128))
        left, top = cx - width // 2, cy - height // 2
        out.paste(src.crop((left, top, left + width, top + height)), (0, 0))
        buf = io.BytesIO()
        out.save(buf, "JPEG", quality=85)
        return buf.getvalue()

    # --- input -------------------------------------------------------------------------------
    def _ydo(self, *args: str) -> None:
        subprocess.run(["ydotool", *args], env=YDOTOOL_ENV, check=True, capture_output=True)

    def _move(self, x: int, y: int) -> None:
        """Move to screenshot-space (x, y). Needs a flat pointer accel profile; see flat_pointer()."""
        px, py = round(x * self.pointer_w / self.width), round(y * self.pointer_h / self.height)
        self._ydo("mousemove", "--absolute", "-x", str(px), "-y", str(py))

    def _keys(self, keys: list[str]) -> None:
        names = [k.lower() for k in keys]
        if any(combo <= set(names) for combo in BLOCKED_COMBOS):
            raise PermissionError(f"blocked shortcut: {keys}")
        unknown = [n for n in names if n not in KEYCODES]
        if unknown:
            raise PermissionError(f"key not supported live: {unknown}")
        codes = [KEYCODES[n] for n in names]
        self._ydo("key", *[f"{c}:1" for c in codes], *[f"{c}:0" for c in reversed(codes)])

    def act(self, code: str) -> None:
        """Run one action in the restricted pyautogui-style form produced by agent.loop._code."""
        if STOP_FILE.exists():
            raise Stopped(f"{STOP_FILE} exists")
        code = code.split("pyautogui.FAILSAFE = False; ", 1)[-1]
        if m := re.fullmatch(r"pyautogui\.(click|doubleClick|rightClick)\((\d+), (\d+)\)", code):
            kind, x, y = m.group(1), int(m.group(2)), int(m.group(3))
            print(f"      live {kind} at ({x}, {y})", flush=True)
            self._move(x, y)
            time.sleep(0.05)
            if kind == "click":
                self._ydo("click", "0xC0")
            elif kind == "doubleClick":
                self._ydo("click", "--repeat", "2", "--next-delay", "60", "0xC0")
            else:
                self._ydo("click", "0xC1")
        elif m := re.fullmatch(r"pyautogui\.write\((.+), interval=0\.005\)", code):
            text = eval(m.group(1), {"__builtins__": {}})  # a repr() string literal we generated
            print(f"      live type {text!r}", flush=True)
            self._ydo("type", "--key-delay", "4", "--", text)
        elif m := re.fullmatch(r"pyautogui\.hotkey\(\*(\[.*\])\)", code):
            keys = eval(m.group(1), {"__builtins__": {}})
            print(f"      live hotkey {keys}", flush=True)
            self._keys(keys)
        elif m := re.fullmatch(r"pyautogui\.press\((.+)\)", code):
            key = eval(m.group(1), {"__builtins__": {}})
            print(f"      live press {key}", flush=True)
            self._keys([key])
        elif m := re.fullmatch(r"pyautogui\.scroll\((-?\d+), (\d+), (\d+)\)", code):
            amount, x, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            self._move(x, y)
            self._ydo("mousemove", "--wheel", "-x", "0", "-y", str(-amount))
        elif m := re.fullmatch(r"time\.sleep\(([\d.]+)\)", code):
            time.sleep(float(m.group(1)))
        else:
            raise PermissionError(f"action not allowed live: {code}")
