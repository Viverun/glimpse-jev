"""jev's interactive prompt: line editing, history, paste, sessions and /commands.

Built on prompt_toolkit, so arrow keys, Home/End, Ctrl+R history search and bracketed paste all work.
Enter sends; Alt+Enter (or Ctrl+J) inserts a newline, and pasted multi-line text arrives intact.
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

import config
import context
import memory
import notify
import sessions
import takeover

# name -> (argument hint, help). The order is the order /help shows them.
COMMANDS: dict[str, tuple[str, str]] = {
    "help": ("", "show this list"),
    "new": ("[title]", "start a fresh session"),
    "sessions": ("", "list saved sessions"),
    "resume": ("[n|id|word]", "continue a saved session (no argument: the most recent)"),
    "rename": ("<title>", "rename this session"),
    "fork": ("", "branch this session into a new one"),
    "delete": ("<n|id>", "delete a saved session"),
    "history": ("[n]", "tasks of this session so far"),
    "context": ("", "how full this session's context window is"),
    "compact": ("[focus]", "summarize older turns now to free up context"),
    "window": ("[256k|1m|max]", "context window size (Luna on Azure allows up to ~922k)"),
    "retry": ("", "run the last task again"),
    "copy": ("", "copy the last report to the clipboard"),
    "export": ("[file]", "write this session as markdown"),
    "remember": ("<text>", "save a preference for every future run"),
    "forget": ("[n|habits|workflows|lessons]", "forget preferences or learned things"),
    "memory": ("[edit]", "show what jev knows; `edit` opens your JEV.md notes"),
    "workflows": ("", "list learned workflows"),
    "lessons": ("", "list taught lessons"),
    "learn": ("", "turn past runs into reusable workflows"),
    "relearn": ("", "rescan this desktop"),
    "status": ("", "settings, stop key and environment"),
    "stopkey": ("[combo]", "show or set the stop hotkey, e.g. ctrl+alt+esc"),
    "popups": ("[on|off|test]", "turn the corner pop-ups on or off, or show a test"),
    "budget": ("<minutes>", "time budget per task"),
    "mode": ("[luna|jev-only]", "planner mode"),
    "confirm": ("[on|off]", "ask before starting each task"),
    "block": ("<word>", "ignore windows whose title contains this"),
    "setup": ("", "connect the planner and Jev: endpoints, keys, models"),
    "glow": ("on|off", "the grey glow around the screen while glimpse works"),
    "doctor": ("", "check that everything jev needs is working"),
    "clear": ("", "clear the screen"),
    "exit": ("", "leave jev (also /quit, Ctrl+D)"),
}
ALIASES = {"quit": "exit", "q": "exit", "bye": "exit", "resume-last": "resume", "flows": "workflows", "taught": "lessons",
           "rescan": "relearn", "profile": "memory", "stop-key": "stopkey", "notifications": "popups", "toast": "popups", "?": "help"}
# Bare words that worked before slash commands existed.
LEGACY = ("remember ", "forget", "memory", "profile", "relearn", "rescan", "learn workflows", "learn flows", "workflows", "flows",
          "taught", "lessons", "exit", "quit", "q", ":q")


# "get this into your memory", "remember that ...", "add this to memory": saving, not a desktop task.
MEMORY_ASK = re.compile(r"\b(get|put|add|save|store|write|keep|note)\b[^.\n]{0,25}\b(in|into|to|in to)\s+(your\s+|jev'?s\s+)?memory\b|"
                        r"^\s*(please\s+)?remember\b|\bdon'?t forget\b|\bremember (this|that|it)\b", re.I)
MEMORY_TAIL = re.compile(r"[,.;:\s-]*(and\s+|then\s+|please\s+)?\b(get|put|add|save|store|write|keep|note)\b[^.\n]{0,25}\b(in|into|to|in to)\s+(your\s+|jev'?s\s+)?memory\b[.!\s]*$", re.I)


def memory_note(text: str) -> str | None:
    """The note to save if `text` is a request to remember something (None if it's a task)."""
    if not MEMORY_ASK.search(text):
        return None
    note = MEMORY_TAIL.sub("", text).strip()
    note = re.sub(r"^\s*(please\s+)?remember( that| this)?[:,]?\s*", "", note, flags=re.I)
    return note if len(note) > 8 else None


class SlashCompleter(Completer):
    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/") or " " in text:
            return
        for name, (hint, help_) in COMMANDS.items():
            if name.startswith(text[1:]):
                yield Completion(name, start_position=-len(text) + 1, display=f"/{name} {hint}".rstrip(), display_meta=help_)


def key_bindings() -> KeyBindings:
    kb = KeyBindings()

    @kb.add("escape", "enter")
    @kb.add("c-j")
    def _(event):  # newline without sending
        event.current_buffer.insert_text("\n")

    return kb


class Repl:
    def __init__(self, args, run_task, console, accent: str, installed_apps):
        self.args, self.run_task, self.console, self.accent, self.installed_apps = args, run_task, console, accent, installed_apps
        self.started = time.time()
        self.session = sessions.new()
        self.last: tuple[str, dict] | None = None  # prompt, result of the latest task
        self.quiet = args.quiet
        self.style = Style.from_dict({"prompt": f"bold {accent}", "bottom-toolbar": "noreverse #888888", "bottom-toolbar.text": "noreverse #888888",
                                      "completion-menu.completion": "bg:#1e1e2a #cccccc", "completion-menu.completion.current": f"bg:{accent} #ffffff",
                                      "completion-menu.meta.completion": "bg:#1e1e2a #777777", "completion-menu.meta.completion.current": f"bg:{accent} #ffffff",
                                      "auto-suggestion": "#555555"})
        hist = memory.DIR / "history"
        hist.parent.mkdir(parents=True, exist_ok=True)
        self.prompt = PromptSession(history=FileHistory(str(hist)), auto_suggest=AutoSuggestFromHistory(), completer=SlashCompleter(),
                                    complete_while_typing=True, key_bindings=key_bindings(), style=self.style,
                                    bottom_toolbar=self.toolbar, mouse_support=False, enable_history_search=False)

    # --- the loop ------------------------------------------------------------------------------
    def toolbar(self):
        title = self.session["title"] or "new session"
        stale = " · ⚠ jev was updated: restart it" if self.stale() else ""
        return f" {title}  ·  context {context.fill(self.session):.0%}  ·  stop: {takeover.stop_combo()}  ·  /help  ·  Alt+Enter newline{stale}"

    def stale(self) -> bool:
        """True once jev's source on disk is newer than the code this process loaded."""
        try:
            return any(f.stat().st_mtime > self.started for f in Path(__file__).parent.glob("*.py"))
        except OSError:
            return False

    def loop(self) -> None:
        interrupted = False
        while True:
            try:
                text = self.prompt.prompt([("class:prompt", "glimpse › ")]).strip()
                interrupted = False
            except KeyboardInterrupt:
                if interrupted:
                    break
                interrupted = True
                self.console.print("[dim]press Ctrl+C again or Ctrl+D to leave[/]")
                continue
            except EOFError:
                break
            if not text:
                continue
            try:
                if self.handle(text) == "exit":
                    break
            except SystemExit:
                pass  # a refused or cancelled task returns to the prompt
        self.console.print("[dim]bye[/]")

    def handle(self, text: str) -> str | None:
        """Run one line: a /command, a !shell command, a legacy word, or a task."""
        if text.startswith("!"):
            subprocess.run(text[1:], shell=True)
            return None
        low = text.lower()
        if not text.startswith("/") and any(low == w.strip() or low.startswith(w) for w in LEGACY):
            text = "/" + text
        if text.startswith("/"):
            name, _, rest = text[1:].partition(" ")
            return self.command(name.lower(), rest.strip())
        note = memory_note(text)
        if note:
            memory.remember(note)
            self.say(f"remembered: {note[:100]}")
            return None
        self.task(text)
        return None

    def task(self, prompt: str) -> None:
        self.auto_compact()  # never start a task on a full window
        self.args.prompt = prompt
        out = self.run_task(self.args, self.session)
        if out:
            result, secs = out
            context.note_usage(self.session, result.pop("prompt_tokens", 0))
            sessions.add_turn(self.session, prompt, result, secs)
            self.last = (prompt, result)
            self.auto_compact()

    def auto_compact(self) -> None:
        if not context.needs_compact(self.session):
            return
        pct = context.fill(self.session)
        if self.compact(f"context {pct:.0%} full: compacting automatically"):
            if not self.args.quiet:
                notify.send("Context compacted", f"Session was {pct:.0%} full; older turns are now a summary.", state="info")

    def compact(self, label: str, focus: str = "") -> bool:
        from hybrid_agent import luna_json
        try:
            with self.console.status(f"[{self.accent}]{label}…"):
                before, after = context.compact(self.session, luna_json, focus)
        except Exception as e:  # e.g. no Azure key: compact() itself falls back, so this is unexpected
            self.console.print(f"[red]compaction failed: {e!r:.120}[/]")
            return False
        self.say(f"compacted: {before:,} → {after:,} tokens ({context.fill(self.session):.0%} of the {context.window():,}-token window)")
        return True

    # --- commands ------------------------------------------------------------------------------
    def say(self, text: str) -> None:
        self.console.print(f"[{self.accent}]✔ {text}[/]")

    def command(self, name: str, rest: str) -> str | None:
        name = ALIASES.get(name, name)
        fn = getattr(self, f"cmd_{name}", None)
        if name not in COMMANDS or fn is None:
            near = [c for c in COMMANDS if c.startswith(name[:2])][:4]
            self.console.print(f"[red]unknown command /{name}[/]" + (f"  [dim]did you mean {', '.join('/' + c for c in near)}?[/]" if near else "  [dim]try /help[/]"))
            return None
        return fn(rest)

    def table(self, title: str, rows: list[tuple], empty: str = "") -> None:
        t = Table.grid(padding=(0, 2))
        t.add_column(style="dim")
        t.add_column()
        for r in rows:
            t.add_row(*r)
        self.console.print(Panel(t if rows else Text(empty), title=f"[bold]{title}[/]", border_style=self.accent, padding=(0, 1)))

    def cmd_help(self, rest):
        self.table("glimpse commands", [(f"/{n} {h}".rstrip(), d) for n, (h, d) in COMMANDS.items()] +
                   [("!<cmd>", "run a shell command"), ("Alt+Enter", "newline in the prompt"), ("Ctrl+R / ↑", "search / walk history"),
                    (takeover.stop_combo(), "stop a running task, from any window")])

    def cmd_exit(self, rest):
        return "exit"

    def cmd_clear(self, rest):
        self.console.clear()

    def cmd_new(self, rest):
        sessions.save(self.session)
        self.session = sessions.new(rest)
        self.last = None
        self.say("new session" + (f": {rest}" if rest else ""))

    def cmd_sessions(self, rest):
        ss = sessions.all_sessions()
        self.table("sessions", [(str(i), Text.assemble((s["title"] or "(untitled)", "bold" if s["id"] == self.session["id"] else ""),
                                                      (f"   {len(s['turns'])} task{'s' * (len(s['turns']) != 1)} · {s['id'][:13]}", "dim")))
                                for i, s in enumerate(ss, 1)], "no saved sessions yet")

    def cmd_resume(self, rest):
        ss = [s for s in sessions.all_sessions() if s["id"] != self.session["id"]]
        s = sessions.find(rest) if rest else (ss[0] if ss else None)
        if not s:
            self.console.print("[red]no such session[/]  [dim]see /sessions[/]")
            return
        sessions.save(self.session)
        self.session = s
        self.last = None
        self.say(f"resumed “{s['title']}” ({len(s['turns'])} tasks)")
        for t in s["turns"][-3:]:
            self.console.print(f"  [dim]{'✔' if t['status'] == 'done' else '✘'}[/] {t['prompt']}")

    def cmd_rename(self, rest):
        if not rest:
            self.console.print("[red]usage: /rename <title>[/]")
            return
        sessions.rename(self.session, rest)
        self.say("renamed")

    def cmd_fork(self, rest):
        self.session = sessions.fork(self.session)
        self.say(f"forked into “{self.session['title']}”")

    def cmd_delete(self, rest):
        s = sessions.find(rest) if rest else None
        if not s:
            self.console.print("[red]usage: /delete <n|id>  (see /sessions)[/]")
            return
        sessions.delete(s["id"])
        if s["id"] == self.session["id"]:
            self.session = sessions.new()
        self.say(f"deleted “{s['title']}”")

    def cmd_history(self, rest):
        n = int(rest) if rest.isdigit() else 15
        self.table("this session", [(t["time"][11:16], Text(f"{'✔' if t['status'] == 'done' else '✘'} {t['prompt'][:90]}  ({t['seconds']}s)"))
                                    for t in self.session["turns"][-n:]], "nothing yet")

    def cmd_context(self, rest):
        s, w = self.session, context.window()
        f = context.fill(s)
        n = round(min(f, 1) * 30)
        bar = Text("█" * n + "░" * (30 - n), style="green" if f < 0.7 else "yellow" if f < context.compact_at() else "red")
        live = len(s["turns"]) - s.get("compacted", 0)
        self.table("context", [
            ("used", Text.assemble(bar, f"  {context.used(s):,} / {w:,} tokens ({f:.0%})")),
            ("session", f"{live} turn(s) in context · {s.get('compacted', 0)} folded into the summary" + (" · summary present" if s.get("summary") else "")),
            ("auto-compact", f"at {context.compact_at():.0%} of the window, keeping the last {context.KEEP_TURNS} turns"),
            ("model limit", f"{context.LIMIT:,} tokens (gpt-6-luna on Azure)  [dim]/window to change[/]"),
        ])

    def cmd_compact(self, rest):
        if len(self.session["turns"]) - self.session.get("compacted", 0) <= context.KEEP_TURNS:
            self.console.print(f"[dim]nothing to compact yet (the last {context.KEEP_TURNS} turns always stay in full)[/]")
            return
        self.compact("Luna is summarizing older turns", rest)

    def cmd_window(self, rest):
        if rest:
            n = context.parse_size(rest)
            if not n or n > context.LIMIT:
                self.console.print(f"[red]use a size between 8k and {context.LIMIT // 1000}k, e.g. /window 256k or /window max[/]")
                return
            config.put("window", n)
            self.auto_compact()
        self.say(f"context window: {context.window():,} tokens")

    def cmd_retry(self, rest):
        if not self.last:
            self.console.print("[red]nothing to retry yet[/]")
            return
        self.task(self.last[0])

    def clipboard(self, text: str) -> bool:
        try:  # wl-copy forks to serve the selection; stdin closes at once
            return subprocess.run(["wl-copy"], input=text.encode(), timeout=3).returncode == 0
        except Exception:
            return False

    def cmd_copy(self, rest):
        if not self.last:
            self.console.print("[red]no report to copy yet[/]")
            return
        r = self.last[1]
        text = (r.get("report") or r.get("progress") or r.get("error") or "").strip()
        self.say("copied the last report" if text and self.clipboard(text) else "nothing copied (no report, or wl-copy missing)")

    def cmd_export(self, rest):
        if not self.session["turns"]:
            self.console.print("[red]nothing to export yet[/]")
            return
        path = Path(rest).expanduser() if rest else Path.home() / f"glimpse-{self.session['id']}.md"
        path.write_text(sessions.export_markdown(self.session))
        self.say(f"wrote {path}")

    def cmd_remember(self, rest):
        if not rest:
            self.console.print("[red]usage: /remember <text>[/]")
            return
        memory.remember(rest)
        self.say("remembered")

    def cmd_forget(self, rest):
        rest = rest.lower()
        if rest == "habits":
            memory.forget_habits()
            self.say("forgot learned habits")
        elif rest in ("workflows", "flows"):
            memory.WORKFLOWS.unlink(missing_ok=True)
            self.say("forgot all learned workflows")
        elif rest in ("lessons", "taught"):
            memory.untaught()
            self.say("forgot all taught lessons")
        else:
            memory.forget(int(rest) if rest.isdigit() else None)
            self.say(f"forgot {'preference ' + rest if rest.isdigit() else 'all preferences'}")

    def cmd_memory(self, rest):
        if rest == "edit":
            memory.DIR.mkdir(parents=True, exist_ok=True)
            memory.NOTES.touch()
            subprocess.run([*(os.environ.get("EDITOR") or "nano").split(), str(memory.NOTES)])
            return
        p = memory.load_profile()
        rows = []
        d = p.get("desktop") or {}
        if d:
            rows += [("desktop", f"{d.get('gnome')} · dock: {', '.join(d.get('dock', []))}"), ("files", f"{p.get('indexed_files', 0)} indexed")]
        known = sorted(f.stem for f in memory.APPS_DIR.glob("*.json")) if memory.APPS_DIR.exists() else []
        if known:
            rows.append(("apps learned", ", ".join(known)))
        rows += [(f"pref {i}", x) for i, x in enumerate(p.get("preferences", []))] + [("account", a) for a in p.get("accounts", [])]
        n = memory.notes()
        rows.append(("JEV.md", f"{len(n.splitlines())} line(s)  [dim]/memory edit[/]" if n else "empty  [dim]/memory edit[/]"))
        past = memory.runs()
        rows.append(("runs", f"{len(past)} remembered, {sum(r['status'] == 'done' for r in past)} done"))
        rows += [(r["time"], Text(f"{'✔' if r['status'] == 'done' else '✘'} {r['task'][:60]}  ({r['seconds']}s)")) for r in past[-5:]]
        rows.append(("sessions", f"{len(sessions.all_sessions())} saved"))
        self.table("glimpse memory", rows)

    def cmd_workflows(self, rest):
        self.table("learned workflows", [(str(i), f"{w['name']}  [dim]({w.get('app') or 'several apps'}, {len(w['steps'])} steps, {w.get('time', '')})[/]")
                                         for i, w in enumerate(memory.load_workflows())], "no workflows learned yet")

    def cmd_lessons(self, rest):
        t = memory.load_taught()
        self.table("taught lessons", [(app, f"{i['lesson']}  [dim]({i['source']}, {i['time']})[/]") for app, items in t.items() for i in items],
                   "nothing taught yet")

    def cmd_learn(self, rest):
        from hybrid_agent import luna_json
        with self.console.status(f"[{self.accent}]Luna is turning past runs into reusable workflows…"):
            made = memory.induce_from_history(luna_json)
        self.say(f"learned {len(made)} workflow(s)" + "".join(f"\n  • {m}" for m in made))

    def cmd_relearn(self, rest):
        p = memory.refresh_profile(self.installed_apps(), [], force=True)
        self.say(f"rescanned the desktop: {p.get('indexed_files')} files indexed")

    def cmd_stopkey(self, rest):
        if not rest:
            self.console.print(f"stop key: [bold]{takeover.stop_combo()}[/]  [dim]change it with /stopkey ctrl+alt+x (needs at least one modifier)[/]")
            return
        try:
            combo = takeover.parse_combo(rest)
        except ValueError as e:
            self.console.print(f"[red]{e}[/]  [dim]use names like ctrl, alt, shift, super, esc, f9, pause, a-z[/]")
            return
        if len(combo) < 2:
            self.console.print("[red]use at least two keys, so typing never stops a run by accident[/]")
            return
        config.put("stop_keys", "+".join(rest.lower().replace("+", " ").split()))
        self.say(f"stop key is now {takeover.stop_combo()}")

    def cmd_popups(self, rest):
        if rest == "test":
            notify.send("glimpse pop-up test", "A translucent card in the bottom-left corner.", state="done")
            return
        if rest in ("on", "off"):
            self.args.quiet = self.quiet = rest == "off"
            config.put("popups", rest == "on")
        self.say(f"pop-ups {'off' if self.args.quiet else 'on'}")

    def cmd_budget(self, rest):
        try:
            self.args.max_minutes = float(rest)
        except ValueError:
            self.console.print(f"time budget: {self.args.max_minutes:g} min per task  [dim]/budget <minutes> to change[/]")
            return
        self.say(f"budget: {self.args.max_minutes:g} min per task")

    def cmd_mode(self, rest):
        if rest in ("luna", "jev-only"):
            self.args.jev_only = rest == "jev-only"
        self.say(f"mode: {'Jev only, no planner' if self.args.jev_only else 'the planner plans, Jev acts'}")

    def cmd_confirm(self, rest):
        if rest in ("on", "off"):
            self.args.confirm = rest == "on"
        self.say(f"confirm before each task: {'on' if self.args.confirm else 'off'}")

    def cmd_block(self, rest):
        if rest:
            self.args.block.append(rest)
        self.say("ignoring windows titled: " + (", ".join(self.args.block) or "(none)"))

    def cmd_status(self, rest):
        a = self.args
        self.table("glimpse status", [
            ("session", f"{self.session['title'] or 'new'}  [dim]{self.session['id']}[/]"),
            ("mode", "Jev only" if a.jev_only else "the planner plans · Jev acts"),
            ("context", f"{context.fill(self.session):.0%} of {context.window():,} tokens · auto-compact at {context.compact_at():.0%}"),
            ("budget", f"{a.max_minutes:g} min · {a.max_steps} steps per task"),
            ("stop key", takeover.stop_combo()),
            ("pop-ups", "off" if a.quiet else "on"),
            ("confirm", "on" if a.confirm else "off"),
            ("blocked", ", ".join(a.block) or "(none)"),
            ("memory", f"{len(memory.runs())} runs · {len(memory.load_profile().get('preferences', []))} preferences · JEV.md {'set' if memory.notes() else 'empty'}"),
        ])

    def cmd_glow(self, rest):
        if rest in ("on", "off"):
            config.put("glow", rest == "on")
        self.console.print(f"screen glow: {'on' if config.get('glow', True) else 'off'}")

    def cmd_setup(self, rest):
        import setup_wizard
        setup_wizard.run(self.console)

    def cmd_doctor(self, rest):
        import shutil
        import toast
        devs = takeover.physical_devices()
        readable = [n for n, p in devs if os.access(p, os.R_OK)]
        checks = [
            ("TypeSafe (Jev) API key", bool(config.setting("typesafe_api_key")), "run /setup"),
            ("planner endpoint and key", bool(config.setting("planner_base_url") and config.setting("planner_api_key")),
             "run /setup (--jev-only works without)"),
            ("keyboard readable (stop key)", bool(readable), "add yourself to the `input` group and log in again"),
            ("ydotool", bool(shutil.which("ydotool")), "install ydotool for cursor movement"),
            ("gtk-launch", bool(shutil.which("gtk-launch")), "needed to open apps"),
            ("wl-copy (/copy)", bool(shutil.which("wl-copy")), "install wl-clipboard"),
            ("display for pop-ups", bool(os.environ.get("DISPLAY")), "XWayland provides DISPLAY on GNOME"),
            ("system python with GTK3", subprocess.run(["/usr/bin/python3", "-c", "import gi;gi.require_version('Gtk','3.0')"], capture_output=True).returncode == 0,
             "install python3-gi and gir1.2-gtk-3.0"),
        ]
        self.table("glimpse doctor", [(Text("✔" if ok else "✘", style="green" if ok else "red"), name if ok else f"{name}  [dim]{fix}[/]") for name, ok, fix in checks])
        if all(ok for _, ok, _ in checks):
            toast.show("glimpse doctor", "Everything looks good.", "done", ms=3000)
