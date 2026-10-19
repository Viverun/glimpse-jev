"""Stop jev with a key combination, from any window.

Reads the physical keyboards (/dev/input/event*, readable by the `input` group) in a background thread.
jev's own input never shows up there: ydotool types through its own virtual device ("ydotoold virtual
device"), and computer-use-linux clicks through the desktop portal, which injects inside the compositor.
Mouse and touchpad are ignored, so you can use them freely while jev works. When the stop combination
(default Ctrl+Alt+Esc; set JEV_STOP_KEYS or `/stopkey`) is held, the main thread gets SIGINT, so even a
long planner call is interrupted, and any modifier keys jev was holding are released.
"""

import os
import re
import select
import signal
import struct
import subprocess
import threading
import time
from pathlib import Path

EVENT = struct.Struct("llHHi")  # struct input_event on 64-bit Linux: timeval, type, code, value
EV_KEY = 1
DEFAULT_STOP = "ctrl+alt+esc"
SKIP = re.compile(r"ydotool|virtual|power button|lid switch|video bus|camera|lens cover|sof-hda|hdmi|headphone|mic", re.I)
MODIFIERS = (29, 97, 42, 54, 56, 100, 125, 126)  # ctrl, shift, alt, super (left and right)
# Key name -> the evdev codes that count as it (left and right variants of a modifier).
KEYS: dict[str, tuple[int, ...]] = {
    "ctrl": (29, 97), "shift": (42, 54), "alt": (56, 100), "super": (125, 126), "esc": (1,), "space": (57,), "tab": (15,),
    "backspace": (14,), "enter": (28,), "pause": (119,), "print": (99,), "scrolllock": (70,), "delete": (111,), "insert": (110,),
    "home": (102,), "end": (107,), "pageup": (104,), "pagedown": (109,),
    **{c: (code,) for c, code in zip("qwertyuiop", range(16, 26))}, **{c: (code,) for c, code in zip("asdfghjkl", range(30, 39))},
    **{c: (code,) for c, code in zip("zxcvbnm", range(44, 51))}, **{str(d): (code,) for d, code in zip(range(1, 10), range(2, 11))},
    "0": (11,), **{f"f{n}": (58 + n,) for n in range(1, 11)}, "f11": (87,), "f12": (88,),
}


def parse_combo(text: str) -> list[tuple[int, ...]]:
    """'ctrl+alt+esc' -> one tuple of acceptable codes per key. Raises ValueError on an unknown key."""
    names = [n.strip().lower() for n in re.split(r"[+\s]+", text) if n.strip()]
    if not names:
        raise ValueError("empty key combination")
    unknown = [n for n in names if n not in KEYS]
    if unknown:
        raise ValueError(f"unknown key(s): {', '.join(unknown)}")
    return [KEYS[n] for n in names]


def stop_combo() -> str:
    """The configured stop combination (env JEV_STOP_KEYS, then ~/.config/jev/config.json, then the default)."""
    if os.environ.get("JEV_STOP_KEYS"):
        return os.environ["JEV_STOP_KEYS"]
    import config
    return config.get("stop_keys") or DEFAULT_STOP


def physical_devices(listing: str | None = None) -> list[tuple[str, str]]:
    """(name, /dev/input/eventN) of real keyboards, mice and touchpads."""
    out = []
    try:
        listing = listing if listing is not None else Path("/proc/bus/input/devices").read_text()
    except OSError:
        return out
    for b in listing.split("\n\n"):
        name = re.search(r'^N: Name="(.*)"', b, re.M)
        handlers = re.search(r"^H: Handlers=(.*)$", b, re.M)
        if not name or not handlers or SKIP.search(name.group(1)):
            continue
        h = handlers.group(1).split()
        ev = next((x for x in h if x.startswith("event")), None)
        if ev and ("kbd" in h or any(x.startswith("mouse") for x in h)):
            out.append((name.group(1), f"/dev/input/{ev}"))
    return out


def release_modifiers() -> None:
    """Release modifier keys jev may have been holding when it was interrupted mid-shortcut."""
    try:
        subprocess.run(["ydotool", "key", *[f"{c}:0" for c in MODIFIERS]], capture_output=True, timeout=3)
    except Exception:
        pass


class Takeover:
    """Context manager: while active, the stop combination stops the run. `reason` says what was pressed."""

    def __init__(self, combo: str | None = None, grace: float = 0.5):
        self.combo_text = combo or stop_combo()
        try:
            self.combo = parse_combo(self.combo_text)
        except ValueError:
            self.combo_text = DEFAULT_STOP
            self.combo = parse_combo(DEFAULT_STOP)
        self.grace = grace  # ignore input right after arming
        self.reason = ""
        self._stop = threading.Event()
        self._fds: dict[int, str] = {}

    def __enter__(self):
        if os.environ.get("JEV_NO_TAKEOVER"):
            return self
        for name, path in physical_devices():
            if "touchpad" in name.lower() or "mouse" in name.lower():
                continue  # pointers never stop a run
            try:
                self._fds[os.open(path, os.O_RDONLY | os.O_NONBLOCK)] = name
            except OSError:
                pass
        if self._fds:
            self._t0 = time.monotonic()
            threading.Thread(target=self._watch, daemon=True).start()
        return self

    def disarm(self) -> None:
        """The run is over: input from here on is not a stop."""
        self._stop.set()

    def __exit__(self, *exc):
        self._stop.set()
        time.sleep(0.05)
        for fd in list(self._fds):
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds.clear()
        if self.reason:
            release_modifiers()

    @property
    def watching(self) -> list[str]:
        return list(self._fds.values())

    def _watch(self) -> None:
        held: set[int] = set()
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select(list(self._fds), [], [], 0.25)
            except (OSError, ValueError):
                return
            for fd in ready:
                try:
                    data = os.read(fd, EVENT.size * 64)
                except OSError:
                    continue
                for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
                    _, _, typ, code, value = EVENT.unpack_from(data, off)
                    if typ != EV_KEY or value == 2:  # auto-repeat changes nothing
                        continue
                    held.add(code) if value else held.discard(code)
                    if time.monotonic() - self._t0 >= self.grace and all(held & set(codes) for codes in self.combo):
                        if self._stop.is_set():  # disarmed meanwhile: the run already ended
                            return
                        self.reason = f"you pressed {self.combo_text}"
                        self._stop.set()
                        os.kill(os.getpid(), signal.SIGINT)  # interrupts the main thread, even mid-request
                        return
