"""Sessions: a named conversation of tasks you can leave and come back to, like Claude Code's.

~/.config/jev/sessions/<id>.json   {id, title, created, updated, turns: [{time, prompt, status, report, seconds}]}

Every task run in a session is saved as a turn. Earlier turns go to the planner as context (see context.py for the window and compaction), so a
follow-up like "now close it" or "do the same for Bob" knows what came before.
"""

import json
import re
import time
import uuid
from pathlib import Path

DIR = Path.home() / ".config" / "jev" / "sessions"


def _path(sid: str) -> Path:
    return DIR / f"{sid}.json"


def new(title: str = "") -> dict:
    now = time.time()
    return {"id": time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4], "title": title, "created": now, "updated": now, "turns": []}


def save(s: dict) -> None:
    if not s["turns"] and not s["title"]:
        return  # an empty session leaves no file behind
    DIR.mkdir(parents=True, exist_ok=True)
    s["updated"] = time.time()
    _path(s["id"]).write_text(json.dumps(s, indent=1))


def load(sid: str) -> dict | None:
    try:
        return json.loads(_path(sid).read_text())
    except Exception:
        return None


def all_sessions() -> list[dict]:
    """Newest first."""
    out = []
    for f in DIR.glob("*.json") if DIR.exists() else []:
        try:
            out.append(json.loads(f.read_text()))
        except Exception:
            continue
    return sorted(out, key=lambda s: s.get("updated", 0), reverse=True)


def find(ref: str) -> dict | None:
    """By list number (1 = newest), id prefix, or a word from the title."""
    ref = ref.strip()
    ss = all_sessions()
    if ref.isdigit() and 1 <= int(ref) <= len(ss):
        return ss[int(ref) - 1]
    return next((s for s in ss if s["id"].startswith(ref)), None) or next((s for s in ss if ref.lower() in s["title"].lower()), None)


def add_turn(s: dict, prompt: str, result: dict, seconds: float) -> None:
    report = (result.get("report") or result.get("progress") or result.get("error") or "").strip()
    s["turns"].append({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "prompt": prompt, "status": result.get("status", ""),
                       "report": report[:6000], "seconds": round(seconds, 1)})
    if not s["title"]:
        s["title"] = title_from(prompt)
    save(s)


def title_from(prompt: str) -> str:
    t = re.sub(r"\s+", " ", prompt).strip()
    return t if len(t) <= 60 else t[:57].rstrip() + "…"


def rename(s: dict, title: str) -> None:
    s["title"] = title.strip()[:80]
    save(s)


def delete(sid: str) -> bool:
    p = _path(sid)
    if p.exists():
        p.unlink()
        return True
    return False


def fork(s: dict) -> dict:
    c = new(s["title"] + " (fork)")
    c["turns"] = [dict(t) for t in s["turns"]]
    c.update({k: s[k] for k in ("summary", "compacted") if k in s})
    save(c)
    return c


def context(s: dict) -> str:
    """What the planner is told about this session: the summary of compacted turns, then the later turns in full,
    numbered oldest first, so "my last command" and "that" can be resolved."""
    turns = s["turns"][s.get("compacted", 0):]
    if not turns and not s.get("summary"):
        return ""
    lines = ["Previous tasks of this session, oldest first:"]
    if s.get("summary"):
        lines.append(f"[summary of older turns]\n{s['summary']}")
    base = s.get("compacted", 0)
    for i, t in enumerate(turns, base + 1):
        out = ("\n" + " " * 4).join((t["report"] or "no report").splitlines())
        lines.append(f"[{i}] User: {t['prompt']}\n    jev ({t['status']}): {out}")
    lines.append(f"Most recent earlier message: [{base + len(turns)}] \"{turns[-1]['prompt'][:200]}\"" if turns else "")
    return "\n".join(x for x in lines if x)


def export_markdown(s: dict) -> str:
    out = [f"# {s['title'] or 'glimpse session'}", "", f"Session `{s['id']}`", ""]
    for t in s["turns"]:
        mark = "✔" if t["status"] == "done" else "✘"
        out += [f"## {mark} {t['prompt']}", f"*{t['time']} · {t['seconds']} s · {t['status']}*", "", t["report"] or "(no report)", ""]
    return "\n".join(out)
