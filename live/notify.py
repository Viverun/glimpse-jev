"""Pop-ups for jev runs: translucent cards in the bottom-left corner (see toast.py), not GNOME notifications.

The title says what happened; the body carries the information.
"""

import re
import time

import toast

_started = time.time()
_last_progress = 0.0


def start() -> None:
    global _started, _last_progress
    _started, _last_progress = time.time(), 0.0


def _took(secs: float) -> str:
    if secs >= 3600:
        return f"{secs / 3600:.1f} h"
    return f"{secs / 60:.0f} min" if secs >= 120 else f"{secs:.0f} s"


def _plain(md: str) -> str:
    t = re.sub(r"\[([^\]]+)\]\((?:https?://|file://)[^)]*\)", r"\1", md or "")  # [text](url) -> text
    t = re.sub(r"\*\*|__|`|^#+\s*|^>\s*", "", t, flags=re.M).strip()
    t = re.sub(r"^\s*[-*]\s+", "• ", t, flags=re.M)
    return re.sub(r"\n{2,}", "\n", t)


def send(title: str, body: str = "", state: str = "jev", urgency: str = "normal", expire_ms: int | None = 6000) -> None:
    """Show a card. Failures and stops stay longer; a missing display just means no pop-up."""
    try:
        toast.show(title, _plain(body)[:280], state)  # every card is capped at ~3 s (toast.MAX_MS)
    except Exception:
        pass


def started(task: str) -> None:
    global _started, _last_progress
    _started, _last_progress = time.time(), 0.0
    send("jev is running", task[:140], state="working", expire_ms=4000)


def progress(text: str, every_s: float = 30) -> None:
    """A chunk of work finished; at most one every `every_s` seconds so long sessions don't flood."""
    global _last_progress
    if text and time.time() - _last_progress >= every_s:
        _last_progress = time.time()
        send(f"Working · {_took(time.time() - _started)} in", text, state="working", expire_ms=8000)


def finished(result: dict, secs: float) -> None:
    """The card says the actual outcome: the report's headline as the title, its first details as the body."""
    status = result.get("status", "")
    lines = [l for l in _plain(result.get("report") or result.get("progress") or "").splitlines() if l.strip()]
    headline, details = (lines[0], lines[1:4]) if lines else ("", [])
    if status == "done":
        send(headline[:110] or "Task complete", "\n".join(details + [f"✔ done in {_took(secs)}"]), state="done")
    else:
        reason = result.get("error") or status
        title = "Stopped" if status in ("stopped", "blocked") else "Couldn't finish"
        send(f"{title} · {_took(secs)}", "\n".join(x for x in (reason, headline) if x), state="stopped", urgency="critical")


def turn(text: str, n: int) -> None:
    """The supervised agent (e.g. Claude) finished a reply; jev read it and is sending the next instruction."""
    send(f"Claude finished turn {n}", text, state="done", expire_ms=9000)
