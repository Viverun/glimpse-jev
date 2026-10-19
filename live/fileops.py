"""The fileop step: exact file operations with GNOME's gio, then the result shown in Files.

Guards: paths must be inside your home folder; nothing is overwritten unless the step says "overwrite": true;
"trash" moves to the trash, nothing is deleted; mkdir creates parents. Errors come back as text for the planner.
"""

import subprocess
from pathlib import Path

OPS = ("move", "copy", "rename", "mkdir", "trash")


def _path(p: str | None, what: str) -> Path:
    if not p:
        raise ValueError(f"{what} is missing")
    full = Path(p).expanduser().resolve()
    home = Path.home().resolve()
    if full != home and home not in full.parents:
        raise ValueError(f"{what} {p} is outside your home folder")
    if full == home:
        raise ValueError(f"{what} cannot be your home folder itself")
    return full


def _gio(*args: str) -> str | None:
    r = subprocess.run(["gio", *args], capture_output=True, text=True, timeout=60)
    return None if r.returncode == 0 else (r.stderr.strip() or f"gio {args[0]} failed")[:200]


def show(paths: list[Path]) -> None:
    """Open Files with these items selected, so you see the result (org.freedesktop.FileManager1.ShowItems)."""
    uris = "[" + ", ".join(f"'{p.as_uri()}'" for p in paths) + "]"
    subprocess.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.FileManager1", "--object-path",
                    "/org/freedesktop/FileManager1", "--method", "org.freedesktop.FileManager1.ShowItems", uris, ""],
                   capture_output=True, timeout=10)


def run(step: dict) -> str | None:
    op = step.get("op")
    if op not in OPS:
        return f"unknown fileop {op!r}: use one of {', '.join(OPS)}"
    try:
        src = _path(step.get("src") or (step.get("dst") if op == "mkdir" else None), "src" if op != "mkdir" else "dst")
        if op == "mkdir":
            src.mkdir(parents=True, exist_ok=True)
            result = src
        elif op == "trash":
            if not src.exists():
                return f"{src} does not exist"
            err = _gio("trash", str(src))
            if err:
                return err
            result = src.parent
        else:
            if not src.exists():
                return f"{src} does not exist"
            raw = str(step.get("dst") or "")
            # rename to a bare name ("final.txt") keeps the file in its folder
            dst = _path(str(src.with_name(raw)) if op == "rename" and "/" not in raw else raw, "dst")
            if dst.is_dir() and op != "rename":
                dst = dst / src.name
            if dst.exists() and not step.get("overwrite"):
                return f"{dst} already exists: not overwritten (set \"overwrite\": true only if the task asks)"
            dst.parent.mkdir(parents=True, exist_ok=True)
            err = _gio("move" if op in ("move", "rename") else "copy", *(["-f"] if step.get("overwrite") else []), str(src), str(dst))
            if err:
                return err
            result = dst
    except (ValueError, OSError, subprocess.TimeoutExpired) as e:
        return str(e)
    if step.get("show", True):
        show([result])
    return None
