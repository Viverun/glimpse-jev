"""jev: computer use on the live GNOME desktop, driven by Jev alone.

  jev                                   interactive session (type /help)
  jev -c                                continue the most recent session
  jev --resume [word]                   pick a saved session to continue
  jev "In Text Editor write 'hello world' and save as ~/notes/hello.txt"
  jev --window "Calc" "Make the header row bold and save"

Put text to type in quotes. Stop anytime with the stop hotkey (default Ctrl+Alt+Esc), Ctrl+C, or:
touch ~/glimse-demo/STOP
"""

import argparse
import configparser
import re
import os
import subprocess
import sys
import time
from pathlib import Path

from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

sys.path.insert(0, str(Path(__file__).parent))
from a11y_agent import STOP_FILE, Desktop, is_terminal  # noqa: E402
from desktop_mcp import clean_portal_screenshots  # noqa: E402
from hybrid_agent import HybridAgent  # noqa: E402
import memory  # noqa: E402
import notify  # noqa: E402
import sessions  # noqa: E402
import config  # noqa: E402
import takeover  # noqa: E402
from jev_agent import BlockedApp, JevAgent, text_spans  # noqa: E402
from livevm import flat_pointer  # noqa: E402
from takeover import Takeover  # noqa: E402
from typesafe_sdk import Choice  # noqa: E402

BLOCKED = ("whatsapp",)
ACCENT = "#7c5cff"
console = Console()


# --- apps --------------------------------------------------------------------------------------
def installed_apps() -> dict[str, str]:
    """Visible .desktop apps: lowercase name -> desktop id."""
    dirs = [Path("/usr/share/applications"), Path.home() / ".local/share/applications",
            Path("/var/lib/flatpak/exports/share/applications"), Path.home() / ".local/share/flatpak/exports/share/applications"]
    apps = {}
    for d in dirs:
        for f in d.glob("*.desktop") if d.exists() else []:
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                cp.read(f, encoding="utf-8")
                e = cp["Desktop Entry"]
            except Exception:
                continue
            if e.get("NoDisplay", "false").lower() == "true" or e.get("Type") != "Application":
                continue
            for key in ("Name", "GenericName"):
                if e.get(key):
                    apps.setdefault(e[key].lower(), f.stem)
    return apps


DEFAULT_BROWSER = ("zen browser", "zen")
OTHER_BROWSERS = ("firefox", "brave", "chrome", "chromium", "epiphany")
WEB_WORDS = re.compile(r"\b(browser|website|web ?page|web site|url|link|search the web|search online|google|youtube|e?-?mail|gmail|inbox|"
                       r"github|wikipedia|online|tab)\b|https?://|www\.|\b[\w-]+\.(com|org|net|io|ai|dev|edu|gov)\b", re.I)


def app_in_prompt(prompt: str, apps: dict[str, str]) -> tuple[str, str] | None:
    p = prompt.lower()
    named_browser = any(b in p for b in OTHER_BROWSERS)
    hits = [(name, did) for name, did in apps.items() if len(name) > 3 and name in p
            and not (name in ("web browser",) and not named_browser)]  # generic "web browser" means Zen
    if hits:
        return max(hits, key=lambda h: len(h[0]))
    if WEB_WORDS.search(prompt) and not named_browser:
        return DEFAULT_BROWSER  # Zen is jev's browser
    return None


# --- UI ----------------------------------------------------------------------------------------
def header(prompt: str, target: str, budget: str = "") -> Panel:
    t = Text()
    t.append("task    ", style="dim"); t.append(prompt + "\n", style="bold")
    t.append("window  ", style="dim"); t.append(target + "\n", style=ACCENT)
    t.append("\nstop    ", style="dim"); t.append(f"{takeover.stop_combo()}  ·  Ctrl+C  ·  touch {STOP_FILE}", style="yellow")
    if budget:
        t.append("\nbudget  ", style="dim"); t.append(budget)
    return Panel(t, title=f"[bold {ACCENT}]◆ jev[/] [dim]computer use · Luna plans · Jev acts[/]", border_style=ACCENT, padding=(0, 1))


def bar(p: float, width: int = 10) -> Text:
    n = round(p * width)
    color = "green" if p >= 0.7 else "yellow" if p >= 0.4 else "red"
    return Text("█" * n + "░" * (width - n), style=color) + Text(f" {p:.2f}", style="dim")


def steps_table(rows: list[dict], status: str) -> Table:
    tb = Table(expand=True, box=None, header_style="dim", pad_edge=False)
    tb.add_column("#", width=3, justify="right", style="dim")
    tb.add_column("action", ratio=1, overflow="ellipsis", no_wrap=True)
    tb.add_column("check", width=16)
    tb.add_column("", width=1)
    tb.add_column("ms", width=6, justify="right", style="dim")
    for r in rows[-16:]:
        label = r["label"]
        if r.get("kind") == "plan":
            tb.add_row("", Text(label, style=f"italic {ACCENT}"), Text(""), Text(""), str(r["ms"]))
            continue
        if r.get("kind") == "update":
            continue  # printed in full above the table
        failed = "✘" in label or r["conf"] < 0.5
        tb.add_row(str(r["step"]), Text(label, style="red" if failed else ""), bar(r["conf"]), Text(""), str(r["ms"]))
    tb.caption = Text(status, style=f"bold {ACCENT}")
    return tb


def summary(result: dict, secs: float, rows: list[dict]):
    ok = result["status"] == "done"
    color = "green" if ok else "red"
    parts = []
    report = (result.get("report") or "").strip()
    if report:
        parts.append(Markdown(report))
    elif result.get("progress"):
        parts.append(Text(result["progress"]))
    if not ok:
        parts.append(Text(f"\n✘ {result['status']}" + (f": {result['error']}" if result.get("error") else ""), style="bold red"))
    acts = [r for r in rows if r.get("kind") not in ("plan", "update")]
    turns = [r for r in rows if r.get("kind") == "update"]
    timing = result.get("timing") or {}
    stats = f"{secs / 60:.1f} min" if secs >= 120 else f"{secs:.1f} s"
    plans = len([r for r in rows if r.get("kind") == "plan"])
    stats += f"  ·  {len(acts)} action{'s' * (len(acts) != 1)}  ·  {plans} plan{'s' * (plans != 1)}"
    if turns:
        stats += f"  ·  {len(turns)} Claude turn{'s' * (len(turns) != 1)}"
    if timing:
        stats += "  ·  " + "  ".join(f"{k} {v['ms'] / 1000:.1f}s" for k, v in timing.items() if v.get("ms", 0) >= 100)
    parts.append(Text("\n" + stats, style="dim"))
    title = "[bold green]✔ done[/]" if ok else "[bold red]✘ not finished[/]"
    return Panel(Group(*parts), title=title, title_align="left", border_style=color, padding=(1, 2))


def duration_minutes(prompt: str) -> float | None:
    """'for 2 hours', 'for 90 minutes', '3h' -> minutes."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m)\b", prompt.lower())
    if not m:
        return None
    v = float(m.group(1))
    return v * 60 if m.group(2).startswith("h") else v


# --- main --------------------------------------------------------------------------------------
def pick_window(agent: JevAgent, desktop: Desktop, prompt: str, own_id: int | None) -> dict | None:
    wins = [w for w in desktop.windows() if w.get("window_id") != own_id and agent.usable(w) and w.get("title")]
    if not wins:
        return None
    crit = {str(i): f"{w['title']} ({w.get('wm_class')})" for i, w in enumerate(wins)}
    r = agent.jev.system_one(state={"task": prompt, "windows": crit},
                             questions={"w": Choice(instructions="Which of `windows` is the one `task` should be done in?", criteria=crit)})
    return wins[int(r.choices["w"].choice)]


def main() -> None:
    ap = argparse.ArgumentParser(prog="glimpse", description="Computer use on the live desktop, driven by Jev.")
    ap.add_argument("prompt", nargs="?", help="what to do; put text to type in quotes (omit for interactive mode)")
    ap.add_argument("--window", help="title substring of the window to work in")
    ap.add_argument("--max-steps", type=int, default=300)
    ap.add_argument("--max-minutes", type=float, default=30, help="time budget per task")
    ap.add_argument("--confirm", action="store_true", help="ask before starting each task")
    ap.add_argument("--jev-only", action="store_true", help="no planner: Jev picks every action itself")
    ap.add_argument("--block", action="append", default=[], help="ignore windows whose title contains this (repeatable)")
    ap.add_argument("--quiet", action="store_true", help="no pop-ups")
    ap.add_argument("-c", "--continue", dest="cont", action="store_true", help="continue the most recent session")
    ap.add_argument("-r", "--resume", nargs="?", const="", metavar="WORD", help="continue a saved session (by number, id or title word)")
    ap.add_argument("--sessions", action="store_true", help="list saved sessions and exit")
    ap.add_argument("--no-session", action="store_true", help="don't save this run to a session")
    ap.add_argument("--stop-key", help="stop hotkey for this run, e.g. ctrl+alt+x (see /stopkey to save one)")
    ap.add_argument("-y", "--yes", action="store_true", help=argparse.SUPPRESS)  # kept for old scripts; now the default
    args = ap.parse_args()
    import atexit
    started = time.time()
    atexit.register(clean_portal_screenshots, started - 1)  # also covers a crash or Ctrl+D mid-task
    if config.get("popups") is False:
        args.quiet = True
    if args.stop_key:
        os.environ["JEV_STOP_KEYS"] = args.stop_key
    from repl import Repl
    repl = Repl(args, run_task, console, ACCENT, installed_apps)
    if args.sessions:
        repl.cmd_sessions("")
        return
    if args.cont or args.resume is not None:
        s = sessions.find(args.resume) if args.resume else (sessions.all_sessions() or [None])[0]
        if not s:
            console.print("[red]no saved session to continue[/]  [dim]start one by just using jev[/]")
            sys.exit(1)
        repl.session = s
    if args.prompt in ("setup", "init"):
        args.prompt = "/setup"
    if args.prompt:  # one task, or one /command
        if args.no_session:
            repl.session = sessions.new()
        repl.handle(args.prompt) if args.prompt.startswith("/") else repl.task(args.prompt)
        return
    console.print(welcome(repl.session))
    repl.loop()
    sessions.save(repl.session)


def welcome(session: dict) -> Panel:
    t = Text()
    t.append("Describe a task on your desktop.\n", style="bold")
    t.append("/help for commands  ·  Alt+Enter for a newline  ·  ", style="dim")
    t.append(f"{takeover.stop_combo()} stops a running task", style="dim")
    if session["turns"]:
        t.append(f"\n\nresumed “{session['title']}”: {len(session['turns'])} earlier task{'s' * (len(session['turns']) != 1)}", style=ACCENT)
    return Panel(t, title=f"[bold {ACCENT}]◆ jev[/] [dim]computer use · Luna plans · Jev acts[/]", border_style=ACCENT, padding=(1, 2))


def run_task(args, session: dict | None = None) -> tuple[dict, float] | None:
    """Run one task. Returns (result, seconds), or None if it was refused or never started."""
    if any(b in args.prompt.lower() for b in BLOCKED):
        console.print(Panel("[bold red]refused[/]: this prompt involves a blocked app (WhatsApp).", border_style="red"))
        sys.exit(2)
    if not config.setting("typesafe_api_key"):
        console.print("[red]No TypeSafe API key. Run [bold]glimpse setup[/bold].[/]")
        sys.exit(2)

    asked = duration_minutes(args.prompt)
    if asked:  # "work on it for 3 hours": that's the budget
        args.max_minutes = asked
        args.max_steps = max(args.max_steps, int(asked * 40))
    with console.status(f"[{ACCENT}]connecting to the desktop…"):
        desktop = Desktop()
        own = next((w for w in desktop.windows() if w.get("focused")), None)
        if own and not is_terminal(own):
            own = None  # only a focused terminal is jev's own window; never type into it
        rows: list[dict] = []
        common = dict(max_steps=args.max_steps, allow_terminals=True, blocked_words=BLOCKED, ignore_titles=tuple(args.block),
                      exclude_window_ids={own["window_id"]} if own else set(), on_step=rows.append)
        agent = JevAgent(desktop, **common) if args.jev_only else HybridAgent(
            desktop, apps=installed_apps(), max_minutes=args.max_minutes, timeboxed=bool(asked), **common)

    proc = None
    target = None
    hybrid = isinstance(agent, HybridAgent)
    guess = app_in_prompt(args.prompt, installed_apps())
    # Hybrid: Luna sees every open window and opens/focuses apps itself; the detected app is only a hint.
    app = None if (args.window or hybrid) else guess
    if args.window:
        target = next((w for w in desktop.windows() if args.window.lower() in (w.get("title") or "").lower() and agent.usable(w)), None)
    elif not app and not hybrid:
        with console.status(f"[{ACCENT}]Jev is picking the window…"):
            target = pick_window(agent, desktop, args.prompt, own and own["window_id"])
    label = target["title"] if target else (f"launch {app[0].title()}" if app else
                                           f"Luna decides{f' (likely {guess[0].title()})' if guess else ''}" if hybrid else "(none)")

    console.print(header(args.prompt, label, f"up to {args.max_minutes / 60:.1f} h" if args.max_minutes >= 60 else ""))
    if not target and not app and not hybrid:
        console.print("[red]No window to work in. Open the app first or name it in the prompt.[/]")
        sys.exit(1)
    if args.confirm and not console.input(f"[{ACCENT}]start?[/] [dim](y/N)[/] ").strip().lower().startswith("y"):
        sys.exit(0)
    STOP_FILE.parent.mkdir(exist_ok=True)
    STOP_FILE.unlink(missing_ok=True)

    t0, wall_start = time.perf_counter(), time.time()
    # Watchdog: whatever hangs, a run never outlives its budget by more than 2 minutes.
    import signal
    def _overtime(*_):
        raise KeyboardInterrupt(f"watchdog: exceeded the {args.max_minutes:g}-minute budget")
    signal.signal(signal.SIGALRM, _overtime)
    signal.alarm(int(args.max_minutes * 60 + 120))
    status = "running"
    # The stop hotkey (default Ctrl+Alt+Esc) stops jev from any window; the mouse and touchpad stay yours to use.
    with flat_pointer(), Takeover() as you, Live(steps_table(rows, status), console=console, refresh_per_second=8, transient=False) as live:
        def on_step(info):
            if info.get("kind") == "update":  # full text, kept above the live table as a log of Claude's turns
                live.console.print(Text.assemble(("● ", "bold green"), (info["label"].split(" · ", 1)[0][2:] + "  ", "bold green"),
                                                 (info["label"].split(" · ", 1)[-1], "")))
            rows.append(info)
            live.update(steps_table(rows, "running"))
            try:  # written as it goes, so a stuck or killed run can still be diagnosed
                save_log(args.prompt, {"status": "running", "history": getattr(agent, "_history", [])[-40:]},
                         time.perf_counter() - t0, rows)
            except Exception:
                pass
        agent.on_step = on_step
        try:
            if app:
                before = {w["window_id"] for w in desktop.windows()}
                live.update(steps_table(rows, f"launching {app[0].title()}…"))
                proc = subprocess.Popen(["gtk-launch", app[1]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                for i in range(30):
                    time.sleep(0.5)
                    wins = desktop.windows()
                    new = [w for w in wins if w["window_id"] not in before and agent.usable(w)]
                    # An app that's already running may just raise its existing window instead of opening one.
                    existing = [w for w in wins if w.get("app_id") == f"{app[1]}.desktop" and agent.usable(w)]
                    if new or (i >= 3 and existing):
                        target = (new or sorted(existing, key=lambda w: not w.get("focused")))[0]
                        break
                if not target:
                    raise RuntimeError(f"{app[0]} did not open a window")
                time.sleep(0.8)
            if not args.quiet:
                notify.started(args.prompt)
            agent.task_window = target
            if target:
                desktop.mcp.call("activate_window", window_id=target["window_id"])
            if isinstance(agent, HybridAgent):
                if not args.quiet:
                    agent.on_event = lambda kind, text: (notify.progress(text) if kind == "progress"
                                                         else notify.turn(text, agent.turns) if kind == "update" else None)
                live.update(steps_table(rows, "recalling…"))
                prof = memory.refresh_profile(installed_apps(), desktop.windows())
                hint = (f"\nLikely app for this task: {guess[0].title()} (desktop id {guess[1]}); open it with open_app "
                        f"unless another open window is clearly the right one.") if guess else ""
                agent.read_only_shell = bool(guess)  # the task names an app: work in it, shell only verifies
                habits = memory.habits_text(args.prompt)
                standing = "\n\n".join(x for x in (memory.notes_text(), sessions.context(session) if session else "") if x)
                agent.context = (standing + "\n\n" if standing else "") + (habits + "\n" if habits else "") + (memory.desktop_context(args.prompt, prof, (target or {}).get("app_id") or (f"{guess[1]}.desktop" if guess else None))
                                 + hint + "\n\n" + memory.recall(agent.jev, args.prompt))
                flow = memory.workflows_for(agent.jev, args.prompt)
                if flow:
                    agent.context += "\n\n" + flow
            result = agent.run(args.prompt)
            you.disarm()
        except KeyboardInterrupt as e:
            result = {"status": "stopped", "error": you.reason or str(e) or "Ctrl+C", "steps": len(rows), "timing": {}}
        except BlockedApp as e:
            result = {"status": "blocked", "error": str(e), "steps": len(rows), "timing": {}}
        except Exception as e:
            result = {"status": "error", "error": repr(e)[:200], "steps": len(rows), "timing": {}}
        if result["status"] in ("stopped", "blocked", "error"):  # keep what happened, for debugging
            result.update(history=getattr(agent, "_history", [])[-60:], failures=getattr(agent, "failures", [])[-10:])
        live.update(steps_table(rows, result["status"]))
    signal.alarm(0)
    secs = time.perf_counter() - t0
    bring_back(desktop, own)
    desktop.mcp.close()
    time.sleep(0.5)  # the portal can finish writing a screenshot just after the call returned
    clean_portal_screenshots(since=wall_start - 1)
    console.print(summary(result, secs, rows))
    if not args.quiet:
        notify.finished(result, secs)
    save_log(args.prompt, result, secs, rows)
    if isinstance(agent, HybridAgent) and not os.environ.get("JEV_NO_LEARN"):  # benchmarks don't write experience
        memory.record(args.prompt, result["status"], agent.ok_steps, agent.failures, secs)
        if result["status"] == "done":
            # Agent Workflow Memory: abstract this verified run into a reusable workflow, in the background
            # (the process waits for it at exit; in interactive mode it runs while you type the next task).
            import threading
            app_id = f"{guess[1]}.desktop" if guess else (agent.task_window or {}).get("app_id")
            threading.Thread(target=memory.induce_workflow, args=(agent.luna_json, args.prompt, list(agent.ok_steps), app_id),
                             daemon=False).start()
    result["prompt_tokens"] = getattr(agent, "last_prompt_tokens", 0)
    return result, secs


def bring_back(desktop: Desktop, own: dict | None) -> None:
    """Raise the terminal jev was started from, so the finished task is the first thing you see."""
    if not own:
        return
    try:
        if any(w.get("window_id") == own["window_id"] for w in desktop.windows()):
            desktop.mcp.call("activate_window", window_id=own["window_id"])
    except Exception:
        pass


def save_log(prompt: str, result: dict, secs: float, rows: list[dict]) -> None:
    """Last run's steps and timing (no screenshots) in ~/.cache/jev, for debugging."""
    import json
    d = Path.home() / ".cache" / "jev"
    d.mkdir(parents=True, exist_ok=True)
    entry = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "prompt": prompt, "seconds": round(secs, 1),
             "steps": [{k: r.get(k) for k in ("step", "label", "conf", "done", "window", "ms")} for r in rows],
             **{k: v for k, v in result.items() if k != "steps"}}
    (d / "last_run.json").write_text(json.dumps(entry, indent=2))


if __name__ == "__main__":
    main()
