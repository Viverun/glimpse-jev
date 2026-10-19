"""Web browsers glimpse works with, and how to make each publish its page content to accessibility tools.

Firefox family (Firefox, Zen, LibreWolf, Floorp, Waterfox): publish the page when started with GNOME_ACCESSIBILITY=1.
Chromium family (Chrome, Chromium, Brave, Edge, Vivaldi, Opera): need ACCESSIBILITY_ENABLED=1 or the
--force-renderer-accessibility flag. Without these, a browser shows glimpse no page elements at all, and every
click has to be found on the screenshot.
"""

import configparser
import os
import re
import shlex
from pathlib import Path

FIREFOX = ("zen", "firefox", "librewolf", "floorp", "waterfox", "mozilla")
CHROMIUM = ("chrome", "chromium", "brave", "edge", "vivaldi", "opera")
CHROMIUM_FLAG = "--force-renderer-accessibility"
# The browsers' own program names (other programs with "chrome" in their name are not browsers).
BINARIES = {"zen": "firefox", "zen-bin": "firefox", "firefox": "firefox", "firefox-bin": "firefox", "librewolf": "firefox",
            "floorp": "firefox", "waterfox": "firefox", "chrome": "chromium", "google-chrome": "chromium",
            "chromium": "chromium", "chromium-browser": "chromium", "brave": "chromium", "brave-browser": "chromium",
            "msedge": "chromium", "vivaldi-bin": "chromium", "opera": "chromium"}
APP_DIRS = [Path("/usr/share/applications"), Path.home() / ".local/share/applications", Path("/var/lib/snapd/desktop/applications"),
            Path("/var/lib/flatpak/exports/share/applications"), Path.home() / ".local/share/flatpak/exports/share/applications"]


def family(app_id: str | None) -> str | None:
    """'firefox' or 'chromium' for a browser's window app id or desktop id, else None."""
    a = (app_id or "").lower()
    if any(k in a for k in FIREFOX):
        return "firefox"
    if any(k in a for k in CHROMIUM):
        return "chromium"
    return None


def is_browser(window: dict | None) -> bool:
    w = window or {}
    return family(w.get("app_id")) is not None or family(w.get("wm_class")) is not None


def env() -> dict:
    """The environment for apps glimpse starts: both browser families publish their page content."""
    return {**os.environ, "GNOME_ACCESSIBILITY": "1", "ACCESSIBILITY_ENABLED": "1"}


def desktop_file(desktop_id: str) -> Path | None:
    for d in APP_DIRS:
        f = d / f"{desktop_id.removesuffix('.desktop')}.desktop"
        if f.exists():
            return f
    return None


def launch_command(desktop_id: str, args: list[str] = ()) -> list[str]:
    """How to start an app. Chromium-family browsers are started from their own Exec line with the accessibility
    flag (gtk-launch cannot add flags); everything else through gtk-launch."""
    if family(desktop_id) == "chromium":
        f = desktop_file(desktop_id)
        if f:
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                cp.read(f, encoding="utf-8")
                exec_line = cp["Desktop Entry"].get("Exec", "")
                cmd = [w for w in shlex.split(exec_line) if not re.fullmatch(r"%[a-zA-Z]", w)]
                if cmd:
                    return cmd + [CHROMIUM_FLAG, *args]
            except (KeyError, ValueError):
                pass
    return ["gtk-launch", desktop_id.removesuffix(".desktop"), *args]


def process_ready(pid: int, fam: str) -> bool:
    """Was this running browser started so that it publishes its pages?"""
    try:
        environ = Path(f"/proc/{pid}/environ").read_bytes().decode(errors="replace").split("\0")
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0")
    except OSError:
        return True  # cannot tell: do not complain
    if fam == "firefox":
        return "GNOME_ACCESSIBILITY=1" in environ
    return "ACCESSIBILITY_ENABLED=1" in environ or CHROMIUM_FLAG in cmdline


def running() -> list[tuple[str, int, str]]:
    """(name, pid, family) of the main process of each running browser."""
    out, seen = [], set()
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            cmd = (proc / "cmdline").read_bytes().split(b"\0")
            exe = os.path.basename(cmd[0].decode(errors="replace"))
            args = b" ".join(cmd[1:]).decode(errors="replace")
        except (OSError, IndexError):
            continue
        fam = BINARIES.get(exe)
        if not fam or "--type=" in args or "-contentproc" in args or exe in seen:  # helpers, not the browser itself
            continue
        seen.add(exe)
        out.append((exe, int(proc.name), fam))
    return out
