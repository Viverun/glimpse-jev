"""Read a supervised agent's complete reply, not just what fits on screen.

Claude Code: its session transcript (~/.claude/projects/*/*.jsonl) has every reply verbatim. The right
session is the one containing the exact instruction jev just sent (other sessions, including the one
driving jev, are ignored).
Any other window (the Claude app, a chat page): a deep accessibility read of the window's text, keeping
everything after jev's last message.
"""

import json
import re
import time
from pathlib import Path

TRANSCRIPTS = Path.home() / ".claude" / "projects"
MAX_CHARS = 40000


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", t or "").strip().lower()


def _user_text(m: dict) -> str:
    c = m.get("content")
    if isinstance(c, str):
        return c
    return " ".join(b.get("text", "") for b in c or [] if isinstance(b, dict) and b.get("type") == "text")


def claude_code_reply(sent: str, within_min: float = 120) -> str | None:
    """Everything Claude Code wrote after the instruction `sent`, from its transcript."""
    key = _norm(sent)[:80]
    if len(key) < 8 or not TRANSCRIPTS.exists():
        return None
    cutoff = time.time() - within_min * 60
    files = sorted((f for f in TRANSCRIPTS.glob("*/*.jsonl") if f.stat().st_mtime >= cutoff),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    for f in files[:12]:
        try:
            lines = f.read_text(errors="replace").splitlines()
        except OSError:
            continue
        entries = []
        for line in lines:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") in ("user", "assistant") and not d.get("isSidechain"):
                entries.append(d)
        start = max((i for i, d in enumerate(entries) if d["type"] == "user" and key in _norm(_user_text(d.get("message", {})))), default=None)
        if start is None:
            continue
        out = []
        for d in entries[start + 1:]:
            m = d.get("message", {})
            if d["type"] == "user":
                c = m.get("content")
                if isinstance(c, str) or any(b.get("type") == "text" for b in c or [] if isinstance(b, dict)):
                    break  # the next human instruction: this reply is over
                continue  # tool results
            for b in m.get("content") or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text", "").strip():
                    out.append(b["text"].strip())
                elif b.get("type") == "tool_use":
                    inp = b.get("input") or {}
                    arg = inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("description") or ""
                    out.append(f"[tool: {b.get('name')} {str(arg)[:160]}]")
        text = "\n\n".join(out)
        return text[-MAX_CHARS:] if text else ""
    return None


def window_reply(mcp, window: dict, sent: str) -> str:
    """Deep read of a window's text; keeps what comes after jev's last message."""
    st = mcp.call("get_app_state", window_id=window["window_id"], include_screenshot=False, max_nodes=2000, max_depth=64)
    chunks, seen = [], set()
    for e in st.get("accessibility_tree", []):
        t = e.get("text")
        if isinstance(t, dict):
            t = t.get("content")
        t = (t or (e.get("name") if e.get("role") in ("label", "heading", "paragraph", "static", "document text", "terminal") else "") or "").strip()
        if t and t not in seen:
            seen.add(t)
            chunks.append(t)
    text = "\n".join(chunks)
    key = (sent or "").strip()[:40]
    if key and key in text:
        text = text[text.rfind(key) + len(key):]
    return text[-MAX_CHARS:]


def read_reply(mcp, window: dict | None, sent: str) -> tuple[str, str]:
    """(source, complete reply text)."""
    reply = claude_code_reply(sent) if sent else None
    if reply:
        return "Claude Code transcript", reply
    if window:
        try:
            return "window text", window_reply(mcp, window, sent)
        except Exception:
            pass
    return "none", ""
