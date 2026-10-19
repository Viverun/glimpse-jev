"""jev's personalization: a desktop profile, user preferences, and memory of past runs.

~/.config/jev/profile.json   installed apps, browser, home folders, accounts seen; refreshed daily
                             + "preferences": things the user told jev to remember
~/.config/jev/memory.jsonl   one line per run: task, steps that worked, failures (lessons)
~/.config/jev/workflows.json reusable workflows with {variables}, induced from verified runs

Before planning, Jev picks the most similar past task (one Choice question); its working steps and
lessons go to the planner along with the profile, so repeated kinds of tasks get faster and more reliable.
"""

import json
import re
import time
from pathlib import Path

from typesafe_sdk import Choice

DIR = Path.home() / ".config" / "jev"
PROFILE = DIR / "profile.json"
MEMORY = DIR / "memory.jsonl"
NOTES = DIR / "JEV.md"
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def load_profile() -> dict:
    try:
        return json.loads(PROFILE.read_text())
    except Exception:
        return {}


def save_profile(p: dict) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    PROFILE.write_text(json.dumps(p, indent=2))


def refresh_profile(apps: dict[str, str], windows: list[dict], force: bool = False) -> dict:
    """Desktop facts, rebuilt at most daily; preferences and accounts are kept."""
    p = load_profile()
    for w in windows:  # accounts show up in window titles, e.g. "Inbox - me@gmail.com - Gmail"
        for e in EMAIL.findall(w.get("title") or ""):
            p.setdefault("accounts", [])
            if e not in p["accounts"]:
                p["accounts"].append(e)
    if force or time.time() - p.get("updated", 0) > 86400:
        home = Path.home()
        p["desktop"] = scan_desktop()
        p["indexed_files"] = build_file_index()
        p.update({
            "updated": time.time(),
            "user": home.name,
            "home": str(home),
            "browser": "Zen Browser (desktop id: zen)",
            "home_folders": sorted(d.name for d in home.iterdir() if d.is_dir() and not d.name.startswith("."))[:40],
            "apps": sorted({n.title() for n in apps})[:150],
        })
    p.setdefault("preferences", [])
    save_profile(p)
    return p


def remember(note: str) -> None:
    p = load_profile()
    p.setdefault("preferences", []).append(note.strip())
    save_profile(p)


def forget(index: int | None = None) -> None:
    p = load_profile()
    if index is None:
        p["preferences"] = []
    elif 0 <= index < len(p.get("preferences", [])):
        p["preferences"].pop(index)
    save_profile(p)


def notes() -> str:
    """JEV.md: free-form standing instructions and facts you keep for jev (edit with /memory edit)."""
    try:
        return NOTES.read_text().strip()
    except OSError:
        return ""


def add_note(text: str) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    with NOTES.open("a") as f:
        f.write(("\n" if NOTES.exists() and NOTES.stat().st_size else "") + f"- {text.strip()}\n")


def notes_text() -> str:
    n = notes()
    return f"Standing notes from the user (JEV.md), always follow them:\n{n}" if n else ""


def profile_text(p: dict) -> str:
    lines = [f"User: {p.get('user')} (home {p.get('home')}). Browser: {p.get('browser')}."]
    if p.get("accounts"):
        lines.append("Accounts: " + ", ".join(p["accounts"]))
    if p.get("home_folders"):
        lines.append("Home folders: " + ", ".join(p["home_folders"]))
    # The installed-apps list is left out on purpose: it is long, and open_app resolves names itself.
    if p.get("preferences"):
        lines.append("User preferences (always follow):\n" + "\n".join(f"- {x}" for x in p["preferences"]))
    return "\n".join(lines)


# --- memory of runs ------------------------------------------------------------------------------
def runs(limit: int = 300) -> list[dict]:
    if not MEMORY.exists():
        return []
    out = []
    for line in MEMORY.read_text().splitlines()[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def record(task: str, status: str, ok_steps: list[dict], failures: list[str], seconds: float,
           category: str = "", app: str = "", plans: int = 0, workflow: str = "") -> dict:
    """Store a run and return its learning report (how it compares with earlier runs of the same kind)."""
    DIR.mkdir(parents=True, exist_ok=True)
    entry = {"time": time.strftime("%Y-%m-%d %H:%M"), "task": task, "status": status,
             "steps": ok_steps, "failures": failures[:6], "seconds": round(seconds, 1),
             "category": category, "app": (app or "").removesuffix(".desktop"), "plans": plans, "workflow": workflow}
    report = learning(entry, runs())
    entry["learnability"] = report["learnability"]
    with MEMORY.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    return report


# --- learning: categories, learnability, improvement ----------------------------------------------
CATEGORIES = {
    "spreadsheet": "spreadsheets: tables, cells, formulas, sorting, charts (LibreOffice Calc...)",
    "document": "documents: writing, formatting, headings, lists, PDF (LibreOffice Writer, Text Editor...)",
    "files": "files and folders: create, move, rename, organize (Files app)",
    "web": "web: open pages, search, read or extract information from websites",
    "email": "email: read, write, send, triage mail (Gmail...)",
    "messages": "chat apps: read or send messages in Telegram, Discord, Slack, Signal...",
    "agent": "supervising another AI agent (Claude, ChatGPT...): give it tasks and read its replies",
    "system": "system and settings: apps, windows, preferences, the desktop itself",
    "question": "a question, a calculation or small talk answered without using the desktop",
    "other": "anything else",
}
APP_CATEGORY = {"libreoffice-calc": "spreadsheet", "libreoffice-writer": "document", "org.gnome.TextEditor": "document",
                "org.gnome.Nautilus": "files", "zen": "web", "telegram-desktop_telegram-desktop": "messages", "firefox": "web", "org.gnome.Settings": "system"}


def categorize(jev, task: str, app: str = "") -> str:
    """The kind of task (one Jev Choice); the app decides when Jev can't."""
    fallback = APP_CATEGORY.get((app or "").removesuffix(".desktop"), "other")
    try:
        a = jev.system_one(state={"task": task, "app": app or "unknown"}, questions={"c": Choice(
            instructions="Which category does `task` belong to?", criteria=dict(CATEGORIES))}).choices["c"]
        return a.choice if a.confidence >= 0.4 else fallback
    except Exception:
        return fallback


def _words(t: str) -> set[str]:
    return set(re.findall(r"[a-z]{3,}", t.lower())) - {"the", "and", "with", "into", "from", "then", "that", "this", "for"}


def similar_runs(entry: dict, past: list[dict]) -> list[dict]:
    """Earlier runs of the same task (same text), else of the same kind (same category and app, similar words)."""
    same = [r for r in past if r["task"].strip().lower() == entry["task"].strip().lower()]
    if same:
        return same
    w = _words(entry["task"])
    def close(r):
        o = _words(r["task"])
        return w and o and len(w & o) / len(w | o) >= 0.35
    return [r for r in past if r.get("category") == entry.get("category") and r.get("app") == entry.get("app")
            and entry.get("category") and close(r)]


def learnability(entry: dict) -> int:
    """0-100: how well this run can be learned from and reused: verified done, clean steps, few replans,
    and a reusable recipe (a workflow) that came out of it or was used."""
    if entry["status"] != "done":
        return 10 if entry.get("steps") else 0
    score = 40
    score += 20 if not entry.get("failures") else max(0, 20 - 7 * len(entry["failures"]))
    plans = entry.get("plans") or 1
    score += 20 if plans <= 1 else 12 if plans <= 2 else 6 if plans <= 4 else 0
    score += 20 if entry.get("workflow") else 10 if len(entry.get("steps") or []) >= 2 else 0
    return min(score, 100)


def learning(entry: dict, past: list[dict]) -> dict:
    """How this run compares with earlier ones of the same kind, and a label for it."""
    prev = [r for r in similar_runs(entry, past) if r.get("status") == "done"]
    rep = {"category": entry.get("category") or "other", "app": entry.get("app") or "", "attempt": len(similar_runs(entry, past)) + 1,
           "learnability": learnability(entry), "label": "first time", "faster": None, "fewer_plans": None}
    if entry["status"] != "done":
        rep["label"] = "not learned (did not finish)"
        return rep
    if not prev:
        return rep
    best_before = min(r["seconds"] for r in prev)
    last = prev[-1]
    rep["faster"] = round(100 * (last["seconds"] - entry["seconds"]) / last["seconds"]) if last["seconds"] else None
    if last.get("plans") and entry.get("plans"):
        rep["fewer_plans"] = last["plans"] - entry["plans"]
    rep["label"] = ("best yet" if entry["seconds"] < best_before else
                    "improved" if (rep["faster"] or 0) >= 10 else "about the same" if (rep["faster"] or 0) > -10 else "slower")
    return rep


def learning_stats() -> list[dict]:
    """Per category: runs, how many finished, mean learnability, and the time trend of finished runs."""
    out = []
    past = runs(2000)
    for cat in CATEGORIES:
        rs = [r for r in past if (r.get("category") or "other") == cat]
        if not rs:
            continue
        done = [r for r in rs if r["status"] == "done"]
        # Only repeats tell whether glimpse got better: each repeated task's latest run against its first run.
        gains = []
        for i, r in enumerate(done):
            first = [x for x in similar_runs(r, done[:i]) if x.get("status") == "done"]
            later = [x for x in done[i + 1:] if similar_runs(x, [r])]
            if first or not later:
                continue
            last = later[-1]
            gains.append(100 * (r["seconds"] - last["seconds"]) / r["seconds"])
        out.append({"category": cat, "runs": len(rs), "done": len(done), "repeated": len(gains),
                    "learnability": round(sum(r.get("learnability", learnability(r)) for r in rs) / len(rs)),
                    "trend": round(sum(gains) / len(gains)) if gains else None,
                    "apps": sorted({r.get("app") for r in rs if r.get("app")})})
    return out


def recall(jev, task: str) -> str:
    """Most similar past task (Jev Choice) -> planner context: its working steps and lessons."""
    past = runs()
    if not past:
        return ""
    # Most recent run per distinct task text, newest first.
    seen, uniq = set(), []
    for r in reversed(past):
        if r["task"] not in seen:
            seen.add(r["task"])
            uniq.append(r)
    uniq = uniq[:150]
    crit = {str(i): r["task"] for i, r in enumerate(uniq)}
    crit["none"] = "None of these is the same kind of task."
    try:
        a = jev.system_one(state={"new_task": task, "past_tasks": crit}, questions={"m": Choice(
            instructions="Which of `past_tasks` is the same kind of task as `new_task` (same app and goal, maybe "
                         "different details)?", criteria=crit)}).choices["m"]
    except Exception:
        return ""
    if a.choice == "none" or a.confidence < 0.4:
        lessons = [f for r in past[-30:] for f in r.get("failures", [])][-5:]
        return ("Lessons from recent runs:\n" + "\n".join(f"- {x}" for x in lessons)) if lessons else ""
    r = uniq[int(a.choice)]
    same = [x for x in past if x["task"] == r["task"]]
    lessons = [f for x in same for f in x.get("failures", [])][-6:]
    parts = [f"A similar past task: {r['task']!r} ({r['status']}, {r.get('seconds')} s)."]
    slow = (r.get("plans") or 0) > 6 or len(r.get("steps") or []) > 15
    if r["status"] == "done" and r.get("steps") and not slow:
        parts.append("Steps that worked then (adapt values to the new task):\n" + json.dumps(r["steps"]))
    elif r["status"] == "done" and r.get("steps"):
        # A slow run's full step list teaches its detours: give the outcome and let the workflow carry the route.
        parts.append(f"That run was slow ({r.get('seconds')} s, {r.get('plans') or '?'} planning rounds, "
                     f"{len(r['steps'])} steps) and went in circles: do NOT repeat its exploration. Use the reusable "
                     "workflow below if one is given, and plan the whole task in one reply.")
    if lessons:
        parts.append("What went wrong before (avoid):\n" + "\n".join(f"- {x}" for x in lessons))
    return "\n".join(parts)


# --- reusable workflows (Agent Workflow Memory) ---------------------------------------------------
WORKFLOWS = DIR / "workflows.json"
LAST_WORKFLOW = ""  # the workflow recalled for the current task

INDUCE = """You extract reusable workflows from computer-use runs on a GNOME desktop (Agent Workflow Memory).
You get a task that was completed and verified, the steps that worked, and the workflows already known.
Extract the commonly reusable sub-routine of this run as ONE workflow:
- "name": what it does, in general terms (e.g. "Save the open LibreOffice sheet in another format")
- "when": the kind of task it serves, without this task's details
- "app": the app id it runs in, or "" if several
- "variables": {"name": "what it stands for"} for every non-fixed value
- "steps": the steps in the same JSON format, with the non-fixed values (file paths, names, typed text, URLs,
  numbers, cell references, email addresses) replaced by {variable} placeholders. Keep invariant values exactly
  as they are (keys, shortcuts, menu and button names, app names, URL patterns).
Drop steps that were detours or unnecessary. A workflow needs at least two steps. Do not repeat a known
workflow: if one already covers this (same goal, same app), or nothing here is reusable, reply {"skip": true}.
Reply with JSON only."""


def load_workflows() -> list[dict]:
    try:
        return json.loads(WORKFLOWS.read_text())
    except Exception:
        return []


def add_workflow(wf: dict) -> bool:
    """Store an induced workflow; False if it is malformed or duplicates a known one."""
    steps = wf.get("steps")
    if wf.get("skip") or not isinstance(steps, list) or len(steps) < 2 or not all(isinstance(s, dict) and s.get("do") for s in steps):
        return False
    wfs = load_workflows()
    key = lambda w: (w.get("name", "").strip().lower(), w.get("app", ""))  # noqa: E731
    if any(key(w) == key(wf) or w.get("steps") == steps for w in wfs):
        return False
    wfs.append({k: wf.get(k) for k in ("name", "when", "app", "variables", "steps")} | {"time": time.strftime("%Y-%m-%d %H:%M")})
    DIR.mkdir(parents=True, exist_ok=True)
    WORKFLOWS.write_text(json.dumps(wfs[-200:], indent=1))
    return True


def save_package(old_name: str, pkg: dict) -> dict:
    """Replace the workflow named old_name (or add a new one) with a revised skill package; keeps a history of
    failures (the newest 8)."""
    wfs = load_workflows()
    prev = next((w for w in wfs if old_name and w.get("name") == old_name), None)
    failures = ((prev or {}).get("failures") or []) + ([pkg.pop("failure")] if pkg.get("failure") else [])
    pkg = {**(prev or {}), **pkg, "failures": failures[-8:], "revisions": ((prev or {}).get("revisions") or 0) + 1,
           "time": time.strftime("%Y-%m-%d %H:%M")}
    wfs = [w for w in wfs if w is not prev] + [pkg]
    DIR.mkdir(parents=True, exist_ok=True)
    WORKFLOWS.write_text(json.dumps(wfs[-200:], indent=1))
    return pkg


def induce_workflow(luna_json, task: str, steps: list[dict], app_id: str | None) -> dict | None:
    """After a verified run: abstract its steps into a workflow with variables (one planner call)."""
    if len([s for s in steps if s.get("do") not in ("wait", "shell")]) < 2:
        return None
    known = [f"- {w['name']} [{w.get('app', '')}]" for w in load_workflows()][-60:]
    text = (f"Task (completed and verified): {task}\nApp: {app_id or '(several)'}\n"
            f"Steps that worked:\n{json.dumps(steps)}\n\nKnown workflows:\n" + ("\n".join(known) or "(none)"))
    try:
        wf = luna_json(INDUCE, text)
    except Exception:
        return None
    if isinstance(wf, dict) and not wf.get("app") and app_id:
        wf["app"] = app_id.removesuffix(".desktop")
    return wf if isinstance(wf, dict) and add_workflow(wf) else None


def induce_from_history(luna_json) -> list[str]:
    """Offline induction (as AWM does over past experience): one workflow per distinct verified run."""
    seen, made = set(), []
    for r in reversed(runs()):
        if r["status"] != "done" or r["task"] in seen:
            continue
        seen.add(r["task"])
        wf = induce_workflow(luna_json, r["task"], r.get("steps") or [], None)
        if wf:
            made.append(wf["name"])
    return made


def workflows_for(jev, task: str) -> str:
    """The stored workflow that fits a new task (one Jev Choice), as planner context. JEV_NO_WORKFLOWS=1 skips it."""
    import os
    wfs = load_workflows()
    if not wfs or os.environ.get("JEV_NO_WORKFLOWS"):
        return ""
    wfs = wfs[-200:]
    crit = {str(i): f"{w['name']} (for: {w.get('when', '')})" for i, w in enumerate(wfs)}
    crit["none"] = "None of these workflows fits the task."
    try:
        a = jev.system_one(state={"new_task": task, "workflows": crit}, questions={"w": Choice(
            instructions="Which of `workflows` does `new_task` need (it would run these steps, with its own values)?",
            criteria=crit)}).choices["w"]
    except Exception:
        return ""
    if a.choice == "none" or a.confidence < 0.5:
        return ""
    w = wfs[int(a.choice)]
    global LAST_WORKFLOW
    LAST_WORKFLOW = w["name"]
    return (f"A reusable workflow for this kind of task (from earlier verified runs; fill in the {{variables}} with "
            f"this task's values, skip steps that don't apply): {w['name']}\n"
            f"variables: {json.dumps(w.get('variables') or {})}\nsteps: {json.dumps(w['steps'])}"
            + (f"\nbackup route if the main one is blocked: {w['backup']}" if w.get("backup") else "")
            + ("\nrecover rules (learned from earlier failures):\n" + "\n".join(f"- {r}" for r in w["recover"]) if w.get("recover") else "")
            + ("\nfailures seen before (avoid): " + "; ".join(w["failures"][-3:]) if w.get("failures") else ""))


# --- deep desktop knowledge ---------------------------------------------------------------------
import subprocess  # noqa: E402

FILES = DIR / "files.txt"
APPS_DIR = DIR / "apps"
SKIP_DIRS = {"node_modules", ".venv", "venv", "__pycache__", "snap", ".git", "site-packages", "dist", "build", ".cache"}
MIME = {"web links": "x-scheme-handler/http", "mail links": "x-scheme-handler/mailto", "text files": "text/plain",
        "PDFs": "application/pdf", "images": "image/png", "folders": "inode/directory", "spreadsheets": "text/csv"}


def _out(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def _screen() -> str:
    """This machine's monitors, e.g. "2880x1620 at 2x scale (1440x810 logical)" (GTK in the system python)."""
    code = ("import gi; gi.require_version('Gdk','3.0'); from gi.repository import Gdk; d=Gdk.Display.get_default(); "
            "print('; '.join(f'{(m:=d.get_monitor(i)).get_geometry().width*m.get_scale_factor()}x{m.get_geometry().height*m.get_scale_factor()} '"
            "f'at {m.get_scale_factor()}x scale ({m.get_geometry().width}x{m.get_geometry().height} logical)' for i in range(d.get_n_monitors())))")
    import os
    out = _out(["env", "GDK_BACKEND=x11", "/usr/bin/python3", "-c", code]) if os.path.exists("/usr/bin/python3") else ""
    return out.strip() or "unknown"


def scan_desktop() -> dict:
    """GNOME facts that help operate this desktop."""
    facts = {"gnome": _out(["gnome-shell", "--version"])}
    fav = _out(["gsettings", "get", "org.gnome.shell", "favorite-apps"])
    facts["dock"] = [x.removesuffix(".desktop") for x in re.findall(r"'([^']+)'", fav)]
    facts["default_apps"] = {k: _out(["xdg-mime", "query", "default", m]).removesuffix(".desktop") for k, m in MIME.items()}
    bm = Path.home() / ".config/gtk-3.0/bookmarks"
    facts["bookmarks"] = [l.split()[0].replace("file://", "") for l in bm.read_text().splitlines() if l.strip()][:30] if bm.exists() else []
    shortcuts = []
    base = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"
    for path in re.findall(r"'([^']+)'", _out(["gsettings", "get", "org.gnome.settings-daemon.plugins.media-keys", "custom-keybindings"])):
        g = lambda k: _out(["gsettings", "get", f"{base}:{path}", k]).strip("'")
        if g("binding"):
            shortcuts.append(f"{g('binding')} -> {g('name')} ({g('command')})")
    facts["custom_shortcuts"] = shortcuts[:20]
    facts["screen"] = _screen()
    return facts


def build_file_index(max_depth: int = 4, limit: int = 60000) -> int:
    """Names of files/folders in home (not contents). Stays local; only matches go to the planner."""
    home, out = Path.home(), []

    def walk(d: Path, depth: int):
        if depth > max_depth or len(out) >= limit:
            return
        try:
            entries = sorted(d.iterdir(), key=lambda p: p.name)
        except (PermissionError, FileNotFoundError):
            return
        for e in entries:
            if e.name.startswith(".") or e.name in SKIP_DIRS or e.is_symlink():
                continue
            out.append(str(e) + ("/" if e.is_dir() else ""))
            if e.is_dir():
                walk(e, depth + 1)

    walk(home, 1)
    DIR.mkdir(parents=True, exist_ok=True)
    FILES.write_text("\n".join(out))
    return len(out)


def find_files(task: str, n: int = 15) -> list[str]:
    """Paths whose names match words in the task, best first."""
    if not FILES.exists():
        return []
    words = [w for w in re.findall(r"[a-z0-9]{3,}", task.lower())
             if w not in {"the", "and", "with", "file", "folder", "open", "save", "send", "mail", "from", "into", "this", "that", "make"}]
    if not words:
        return []
    scored = []
    for line in FILES.read_text().splitlines():
        name = Path(line.rstrip("/")).name.lower().replace("_", " ").replace("-", " ")
        hits = sum(w in name for w in words)
        if hits:
            scored.append((hits, -line.count("/"), line))
    return [l for *_, l in sorted(scored, reverse=True)[:n]]


# --- per-app knowledge -----------------------------------------------------------------------------
def _app_file(app_id: str) -> Path:
    return APPS_DIR / (re.sub(r"[^\w.-]", "_", app_id or "unknown") + ".json")


def learn_app(app_id: str, title: str, elements: list[dict], worked: list[dict] | None = None, dialog: str | None = None) -> None:
    """Accumulate what an app looks like and what worked in it (not during benchmarks: JEV_NO_LEARN)."""
    import os
    if not app_id or os.environ.get("JEV_NO_LEARN"):
        return
    APPS_DIR.mkdir(parents=True, exist_ok=True)
    f = _app_file(app_id)
    k = json.loads(f.read_text()) if f.exists() else {"app_id": app_id, "controls": {}, "worked": [], "dialogs": []}
    k["last_title"] = title
    for e in elements:
        name = (e.get("name") or "").strip()
        if name and e.get("role") in ("push button", "button", "toggle button", "menu", "menu item", "check box",
                                       "combo box", "page tab", "radio button", "check menu item", "link"):
            key = f"{e['role']}: {name}"[:80]
            k["controls"][key] = k["controls"].get(key, 0) + 1
    if len(k["controls"]) > 400:  # keep the most often seen controls
        k["controls"] = dict(sorted(k["controls"].items(), key=lambda x: -x[1])[:300])
    for s in worked or []:
        if s.get("do") in ("key", "goto", "click", "save_as"):
            entry = {x: s[x] for x in ("do", "key", "url", "target", "path") if x in s}
            if entry not in k["worked"]:
                k["worked"].append(entry)
    k["worked"] = k["worked"][-60:]
    if dialog and dialog not in k["dialogs"]:
        k["dialogs"].append(dialog)
    f.write_text(json.dumps(k, indent=1))


# Curated, verified how-tos per app: the fastest reliable way to do common things.
APP_TIPS = {
    "libreoffice-calc": [
        "Select a cell or range: one select step, {\"do\": \"select\", \"range\": \"{RANGE}\"} (it verifies the selection; never "
        "type into the Name Box yourself).",
        "Fill a column with one formula: select EXACTLY the data rows (select step), {COL}2:{COL}{LAST} with LAST = number of data "
        "rows + 1 (5 data rows -> D2:D6; never D1000: extra rows become zeros), type the formula for row 2 "
        "(e.g. =B2*C2) then key alt+enter (relative refs adjust per row).",
        "Header text: select {COL}1, type the header, key enter.",
        "Bold a row: select A1:{LASTCOL}1, key ctrl+b (once).",
        "Never use backspace on cells (it opens a 'Delete Contents' dialog); use delete.",
        "Verify an ODS: header bold -> unzip -p F.ods content.xml | grep -c 'fo:font-weight=\"bold\"' (>0); "
        "values -> python3 -c \"import pandas as pd; print(pd.read_excel('F.ods', engine='odf'))\". "
        "XLSX: python3 -c \"import pandas as pd; print(pd.read_excel('F.xlsx'))\".",
        "Sort the data by one column: select {COL}2, the column whose header is the sort field (count the header row from A), then "
        "click the menu item 'Sort Descending' or 'Sort Ascending' (Data menu): it sorts the whole table and keeps the header.",
        "Save in any format: one save_as step with the full path (.ods/.xlsx/.csv) does the whole dialog.",
        "Open a file: one open_file step with its path (the CSV Text Import question is answered automatically).",
    ],
    "org.gnome.TextEditor": [
        "It opens with an empty document that already has the keyboard: just type (never click New Tab first: the text "
        "would go to the other tab and an empty file gets saved). Save: one save_as step with the full path.",
        "To replace a document's text: key ctrl+a, then type. To read a file's content use shell cat, not the window.",
        "New tab: ctrl+t. The file dialog is the GNOME portal: always use the save_as step, never type paths into it.",
    ],
    "org.gnome.Nautilus": [
        "Go to a folder: key ctrl+l, type the full path, enter.",
        "New folder: key ctrl+shift+n, type the name, enter. Rename: select, key F2, type, enter.",
        "Move a file: select it (click it), key ctrl+x, go to the target folder, key ctrl+v.",
        "Select several files: click the first, then key ctrl+a for all, or type part of a name to jump to it.",
        "Files ignores ctrl+w for closing: use key alt+f4. Verify file moves with a shell step (ls).",
    ],
    "zen": [
        "Use goto steps for URLs. New tab ctrl+t, close tab ctrl+w, back alt+left, find ctrl+f.",
        "Web pages expose little to the accessibility tree: read them from the screenshot and visible text.",
    ],
    "gmail": [
        "Send mail: goto https://mail.google.com/mail/?view=cm&fs=1&to=ADDR&su=SUBJ&body=BODY then click the blue Send button.",
    ],
    "claude": [
        "Type the instruction in the message box and press enter; then wait_until Claude has finished responding.",
        "Permission prompts: approve immediately (Allow / Always allow).",
    ],
}


TAUGHT = DIR / "taught.json"
HARNESS = DIR / "harness.json"  # adopted always-on planner notes (Harness²-style harnessing)


def _overlay() -> dict:
    """A candidate harness being probed (bench/harness2.py): JEV_HARNESS_OVERLAY=path to {notes, lessons}."""
    import os
    path = os.environ.get("JEV_HARNESS_OVERLAY")
    try:
        return json.loads(Path(path).read_text()) if path else {}
    except Exception:
        return {}


def harness_notes() -> list[str]:
    """Always-on planner notes: the adopted harness plus any candidate overlay. JEV_NO_HARNESS=1 skips the adopted ones."""
    import os
    notes = []
    if not os.environ.get("JEV_NO_HARNESS"):
        try:
            notes = json.loads(HARNESS.read_text()).get("notes", [])
        except Exception:
            pass
    ov = _overlay()
    drop = set(ov.get("notes_remove", []))
    return [n for n in dict.fromkeys(notes + ov.get("notes", [])) if n not in drop]


def adopt_harness(notes_add: list[str], notes_remove: list[str] = ()) -> None:
    try:
        h = json.loads(HARNESS.read_text())
    except Exception:
        h = {"notes": []}
    h["notes"] = [n for n in dict.fromkeys(h.get("notes", []) + list(notes_add)) if n not in set(notes_remove)][-40:]
    DIR.mkdir(parents=True, exist_ok=True)
    HARNESS.write_text(json.dumps(h, indent=1))


def load_taught() -> dict:
    """Lessons taught by demonstration (e.g. by Claude using computer use on this desktop), per app.
    JEV_NO_TAUGHT=1 ignores them (for before/after measurements)."""
    import os
    if os.environ.get("JEV_NO_TAUGHT"):
        return {}
    try:
        return json.loads(TAUGHT.read_text())
    except Exception:
        return {}


def teach(app: str, lesson: str, source: str = "claude") -> None:
    """Add a verified lesson for an app id (or 'general'); shown to the planner like the built-in how-tos."""
    t = load_taught()
    items = t.setdefault(app.removesuffix(".desktop"), [])
    if not any(i["lesson"] == lesson for i in items):
        items.append({"lesson": lesson, "source": source, "time": time.strftime("%Y-%m-%d %H:%M")})
    DIR.mkdir(parents=True, exist_ok=True)
    TAUGHT.write_text(json.dumps(t, indent=1))


def untaught(app: str | None = None) -> None:
    t = load_taught()
    if app is None:
        t = {}
    else:
        t.pop(app.removesuffix(".desktop"), None)
    TAUGHT.write_text(json.dumps(t, indent=1))


def tips_for(app_id: str | None, task: str = "") -> list[str]:
    key = (app_id or "").removesuffix(".desktop")
    taught = load_taught()
    ov = _overlay().get("lessons", {})
    out = list(APP_TIPS.get(key, [])) + [i["lesson"] for i in taught.get(key, [])] + [i["lesson"] for i in taught.get("general", [])] \
        + list(ov.get(key, [])) + list(ov.get("general", []))
    low = task.lower()
    if "gmail" in low or "mail" in low:
        out += APP_TIPS["gmail"]
    if "claude" in low:
        out += APP_TIPS["claude"]
    return out


def app_knowledge(app_ids: list[str], task: str = "") -> str:
    parts = []
    tips = [t for a in dict.fromkeys(app_ids) for t in tips_for(a, task)]
    if tips:
        parts.append("How-tos for this desktop (verified, fastest):\n" + "\n".join(f"- {t}" for t in dict.fromkeys(tips)))
    for a in dict.fromkeys(x for x in app_ids if x):
        f = _app_file(a)
        if not f.exists():
            continue
        k = json.loads(f.read_text())
        top = [c for c, _ in sorted(k["controls"].items(), key=lambda x: -x[1])[:60]]
        lines = [f"Known about {a}:"]
        if top:
            lines.append("  controls seen: " + "; ".join(top))
        if k.get("worked"):
            lines.append("  steps that worked here: " + json.dumps(k["worked"][-15:]))
        if k.get("dialogs"):
            lines.append("  dialogs: " + "; ".join(k["dialogs"]))
        parts.append("\n".join(lines))
    return "\n".join(parts)


def desktop_context(task: str, profile: dict, task_app: str | None) -> str:
    """Everything the planner should know about this desktop for this task."""
    facts = profile.get("desktop") or {}
    lines = [profile_text(profile)]
    if facts:
        lines.append(f"Desktop: {facts.get('gnome')}; {facts.get('screen')}. Dock: {', '.join(facts.get('dock', []))}.")
        lines.append("Default apps: " + ", ".join(f"{k} -> {v}" for k, v in facts.get("default_apps", {}).items() if v))
        if facts.get("bookmarks"):
            lines.append("Files bookmarks: " + ", ".join(facts["bookmarks"]))
        if facts.get("custom_shortcuts"):
            lines.append("Custom shortcuts: " + "; ".join(facts["custom_shortcuts"]))
    files = find_files(task)
    if files:
        lines.append("Files on disk matching the task:\n" + "\n".join(f"- {f}" for f in files))
    apps = [task_app] if task_app else []
    know = app_knowledge(apps or [None], task)
    if know:
        lines.append(know)
    return "\n".join(lines)


# --- the user's own habits (local summaries only) -------------------------------------------------
HABITS = DIR / "habits.json"
PRIVATE_SITES = ("whatsapp",)


def learn_habits() -> dict:
    """Summaries of what the user does on this desktop: apps per file type, busiest folders, most-visited
    sites (domains only), most-used shell command names (never full command lines)."""
    import collections
    import shutil
    import sqlite3
    import tempfile
    import urllib.parse
    import xml.etree.ElementTree as ET
    home = Path.home()
    h = {"updated": time.time()}
    # 1) Recently used files: which app opens which file types, busiest folders.
    try:
        root = ET.parse(home / ".local/share/recently-used.xbel").getroot()
        by_ext, folders = collections.defaultdict(collections.Counter), collections.Counter()
        for b in root.findall("bookmark"):
            path = urllib.parse.unquote(urllib.parse.urlparse(b.get("href", "")).path)
            if not path.startswith(str(home)):
                continue
            ext = Path(path).suffix.lower() or "(folder)"
            folders[str(Path(path).parent).replace(str(home), "~")] += 1
            for app in b.iter("{http://www.freedesktop.org/standards/desktop-bookmarks}application"):
                name = app.get("name", "?")
                if name.startswith(("xdg-desktop-portal", "gnome-shell", "gtk-", "org.freedesktop")):
                    continue  # system components recorded as "apps", not what the user opened the file with
                by_ext[ext][name] += int(app.get("count", "1"))
        h["apps_by_file_type"] = {e: c.most_common(1)[0][0] for e, c in by_ext.items() if c}
        h["busy_folders"] = [f for f, _ in folders.most_common(15)]
    except Exception as e:
        h["recent_error"] = repr(e)[:100]
    # 2) Zen history: most-visited sites by domain (copy the DB: the browser keeps it locked).
    try:
        db = next((Path(home / ".config/zen").glob("*/places.sqlite")), None)
        if db:
            with tempfile.TemporaryDirectory() as tmp:
                shutil.copy(db, Path(tmp) / "p.sqlite")
                con = sqlite3.connect(Path(tmp) / "p.sqlite")
                rows = con.execute("SELECT url, visit_count FROM moz_places WHERE visit_count > 0").fetchall()
                con.close()
            sites = collections.Counter()
            for url, n in rows:
                host = urllib.parse.urlparse(url).hostname or ""
                if host and not any(p in host for p in PRIVATE_SITES):
                    sites[host.removeprefix("www.")] += n
            h["top_sites"] = [s for s, _ in sites.most_common(25)]
    except Exception as e:
        h["sites_error"] = repr(e)[:100]
    # 3) Shell: most-used command names only.
    try:
        lines = (home / ".zsh_history").read_bytes().decode("utf-8", "replace").splitlines()[-5000:]
        cmds = collections.Counter()
        for line in lines:
            cmd = line.split(";", 1)[-1].strip() if line.startswith(":") else line.strip()
            word = cmd.split()[0] if cmd.split() else ""
            if word and re.fullmatch(r"[\w.+-]+", word) and len(word) < 30:
                cmds[word] += 1
        h["top_commands"] = [c for c, _ in cmds.most_common(20)]
    except Exception as e:
        h["shell_error"] = repr(e)[:100]
    DIR.mkdir(parents=True, exist_ok=True)
    HABITS.write_text(json.dumps(h, indent=1))
    return h


def load_habits() -> dict:
    try:
        h = json.loads(HABITS.read_text())
        return h if time.time() - h.get("updated", 0) < 7 * 86400 else learn_habits()
    except Exception:
        return learn_habits()


def habits_text(task: str) -> str:
    h = load_habits()
    low = task.lower()
    lines = []
    if h.get("apps_by_file_type"):
        lines.append("The user opens: " + ", ".join(f"{e} -> {a}" for e, a in list(h["apps_by_file_type"].items())[:12]))
    if h.get("busy_folders"):
        lines.append("The user's busiest folders: " + ", ".join(h["busy_folders"][:10]))
    if h.get("top_sites") and any(w in low for w in ("web", "site", "mail", "pr", "repo", "github", "browser", "online", "docs", "my ", "check", "open")):
        lines.append("The user's most-visited sites (use these for 'my email/repo/docs'): " + ", ".join(h["top_sites"][:15]))
    if h.get("top_commands") and any(w in low for w in ("terminal", "command", "shell", "run", "install", "git", "build", "test")):
        lines.append("The user's most-used commands: " + ", ".join(h["top_commands"]))
    return ("What this user usually does:\n" + "\n".join(f"- {l}" for l in lines)) if lines else ""


def forget_habits() -> None:
    HABITS.unlink(missing_ok=True)
