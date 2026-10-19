"""Jev-only computer-use agent on the live desktop (no GPT).

Each step:
  1. One computer-use-linux call: the task window's accessibility tree + a screenshot of that window only.
  2. Code builds the candidate actions: click each visible control, press common keys, or type a text
     span taken from the task instruction. Jev never has to invent text.
  3. One Jev request: Choice picks the next action, Noul judges whether the task is already complete.
  4. The cursor moves to the element and its AT-SPI action fires (pointer click as fallback);
     keys and text go only to the task window, never to a terminal.
"""

import base64
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

from typesafe_sdk import Choice, Image, Noul, TypeSafeClient

from a11y_agent import STOP_FILE, Desktop, describe, is_terminal
from gtk_dialog import GtkFileDialog

ACTIONABLE = {
    "push button", "button", "toggle button", "check box", "radio button", "menu item", "check menu item",
    "radio menu item", "menu", "combo box", "text", "entry", "spin button", "list item", "page tab",
    "tree item", "link", "table cell",
    # GTK4 roles: text fields, file icons/rows, switches
    "text box", "grid cell", "row", "cell", "switch", "toggle", "search box", "tab",
}
KEYS = ["ctrl+l", "ctrl+t", "ctrl+w", "alt+left", "f5", "ctrl+f", "enter", "escape", "tab", "shift+space", "ctrl+s", "ctrl+shift+s", "ctrl+a", "ctrl+b", "ctrl+home", "shift+end",
        "shift+ctrl+end", "ctrl+o", "down", "up", "delete"]
MAX_CANDIDATES = 250
# Multi-key actions offered as one choice: Jev recognizes the right one, it does not plan key sequences.
MACROS = {
    "select the whole header row (first row of the sheet)": ["ctrl+home", "shift+end"],
    "select all data on the sheet": ["ctrl+home", "shift+ctrl+end"],
    "select the current column": ["ctrl+space"],
}


def _min_size(jpeg: bytes, side: int = 256, max_aspect: float = 3.0) -> bytes:
    """Pad screenshots Jev would reject: tiny popups, or very thin ones (address-bar dropdowns, menus)."""
    import io

    from PIL import Image as PILImage
    img = PILImage.open(io.BytesIO(jpeg)).convert("RGB")
    w, h = max(side, img.width), max(side, img.height)
    w, h = max(w, round(h / max_aspect)), max(h, round(w / max_aspect))
    if (w, h) == img.size:
        return jpeg
    canvas = PILImage.new("RGB", (w, h), (128, 128, 128))
    canvas.paste(img, (0, 0))
    out = io.BytesIO()
    canvas.save(out, "JPEG", quality=80)
    return out.getvalue()


def text_spans(task: str) -> list[str]:
    """Strings the agent may type: quoted text (exactly as written), paths and file names from the instruction."""
    quoted = [a or b for a, b in re.findall(r"'([^']+)'|\"([^\"]+)\"", task)]
    urls = [u.rstrip(".,)") for u in re.findall(r"(https?://\S+|www\.\S+|\b[\w-]+(?:\.[\w-]+)*\.(?:com|org|net|io|ai|dev|edu|gov)(?:/\S*)?)", task)]
    paths = [p.rstrip(".,") for p in re.findall(r"(/[\w./-]+)", task)]

    paths = [p for p in paths if not any(p in u for u in urls)]  # "/path" inside a URL isn't a file path
    names = [Path(p).name for p in paths]
    return list(dict.fromkeys(s for s in quoted + urls + paths + names if s))


def is_url(s: str) -> bool:
    return bool(re.match(r"(https?://|www\.)|^[\w-]+(\.[\w-]+)*\.(com|org|net|io|ai|dev|edu|gov)(/|$)", s))


class BlockedApp(Exception):
    pass


class JevAgent:
    def __init__(self, desktop: Desktop, max_steps: int = 40, debug_dir: Path | None = None, done_threshold: float = 0.8,
                 exclude_window_ids: set[int] | None = None, blocked_words: tuple[str, ...] = ("whatsapp",),
                 allow_terminals: bool = False, on_step=None, ignore_titles: tuple[str, ...] = ()):
        self.d, self.max_steps, self.debug_dir, self.done_threshold = desktop, max_steps, debug_dir, done_threshold
        self.exclude_window_ids = exclude_window_ids or set()
        self.blocked_words = tuple(w.lower() for w in blocked_words)
        self.allow_terminals = allow_terminals
        self.ignore_titles = tuple(t.lower() for t in ignore_titles)  # skipped quietly (unlike blocked apps)
        self.on_step = on_step or (lambda info: print(
            f"  step {info['step']}: {info['label'][:100]}  (conf {info['conf']:.2f}, P(task done) {info['done']:.2f})", flush=True))
        self.jev = __import__('config').jev_client(headers={"TypeSafe-Danger-Zone-Images": "true"}, timeout=30)
        self.ms, self.calls = defaultdict(float), defaultdict(int)
        self.task_window: dict | None = None
        self.gtk = GtkFileDialog(desktop.mcp, self.jev)
        if debug_dir:
            debug_dir.mkdir(parents=True, exist_ok=True)

    def _step_ms(self) -> int:
        total = sum(self.ms.values())
        ms, self._last_total = total - getattr(self, "_last_total", 0.0), total
        return round(ms)

    def _t(self, name, fn, *a, **k):
        t = time.perf_counter()
        try:
            return fn(*a, **k)
        finally:
            self.ms[name] += (time.perf_counter() - t) * 1000
            self.calls[name] += 1

    def observe(self, need_tree: bool = True):
        """One get_app_state call for the task window: tree (600 nodes) + screenshot together.

        After keys or typing in the same window, the element list is reused and only the screenshot
        is refreshed: positions don't move without a window change, and Jev sees content changes on the image.
        """
        wins = self._t("windows", self.d.windows)
        focused = next((w for w in wins if w.get("focused")), None)
        # Follow focus to new windows of the task app (dialogs); never to terminals or other apps.
        if focused and self.is_blocked(focused):
            raise BlockedApp(f"blocked app in focus: {focused.get('title')!r}")
        # Portal dialogs (GTK file choosers) run in another process but report the task app's app_id.
        if focused and self.usable(focused) and (
            self.task_window is None or getattr(self, "follow_any_focus", False)
            or focused.get("pid") == self.task_window.get("pid")
            or (focused.get("app_id") and focused.get("app_id") == self.task_window.get("app_id"))
        ):
            self.task_window = focused
        if not self.task_window:
            return [], None
        # Always work from the window's current info (titles change, e.g. after saving).
        cur = next((w for w in wins if w.get("window_id") == self.task_window["window_id"]), None)
        if cur is None:  # the task window closed: continue in whatever usable window has focus
            self.task_window = focused if focused and self.usable(focused) else None
            if not self.task_window:
                return [], None
        elif not cur.get("focused"):
            # Screenshots only work for the focused window: bring the task window back first.
            try:
                self.d.mcp.call("activate_window", window_id=cur["window_id"])
                time.sleep(0.25)
                cur = {**cur, "focused": True}
            except Exception:
                pass
            self.task_window = cur
        else:
            self.task_window = cur
        app = self.task_window.get("app_id")
        layout = tuple(sorted((w["window_id"], w.get("title"), w.get("focused")) for w in wins
                              if w.get("pid") == self.task_window["pid"] or (app and w.get("app_id") == app)))
        reuse = not need_tree and layout == getattr(self, "_layout", None) and getattr(self, "_els", None) is not None
        state = self._t("observe", self.d.mcp.call, "get_app_state", window_id=self.task_window["window_id"],
                        include_screenshot=True, format="jpeg", quality=70, max_width=960,
                        max_nodes=1 if reuse else 600, max_depth=20)
        self._last_meta = state.get("screenshot")
        self._fresh_tree = not reuse
        if reuse:
            self.calls["tree_reused"] += 1
        else:
            self._els = self.d.elements(self.task_window["pid"], state=state, wins=wins)
            self._layout = layout
        shot = _min_size(base64.b64decode(state["_images"][0])) if state.get("_images") else None
        return self._els, shot

    def is_blocked(self, w: dict) -> bool:
        text = f"{w.get('title') or ''} {w.get('app_id') or ''} {w.get('wm_class') or ''}".lower()
        return any(b in text for b in self.blocked_words)

    def usable(self, w: dict) -> bool:
        """A window the agent may act in: not blocked, not excluded (the CLI's own terminal)."""
        if self.is_blocked(w) or w.get("window_id") in self.exclude_window_ids:
            return False
        if any(t in (w.get("title") or "").lower() for t in self.ignore_titles):
            return False
        return self.allow_terminals or not is_terminal(w)

    def _portal_dialog(self) -> bool:
        w = self.task_window or {}
        return w.get("wm_class") == "org.gnome.Nautilus" and "file" in (w.get("title") or "").lower()

    def candidates(self, task: str, els: list[dict], repeated: Counter) -> list[dict]:
        cands = []
        for e in els:
            if e["role"] in ACTIONABLE:
                cands.append({"type": "click", "el": e, "label": f"click {describe(e)}"})
        for k in KEYS:
            cands.append({"type": "key", "key": k, "label": f"press {k}"})
        for name, keys in MACROS.items():
            cands.append({"type": "macro", "keys": keys, "label": name})
        portal = self._portal_dialog()
        for s in text_spans(task):
            # In a GNOME file dialog typed paths are ignored and typed names land in the last-used folder:
            # offer only the save action, which navigates to the right folder.
            if not (portal and (s.startswith("/") or any(Path(x).name == s for x in text_spans(task) if x.startswith("/")))):
                cands.append({"type": "type", "text": s, "label": f"type {s!r} into the focused field"})
            if is_url(s):
                cands.append({"type": "goto", "text": s, "label": f"go to {s} in the browser (address bar)"})
            if s.startswith("/") and portal:
                # GNOME portal (GTK4) file dialog: navigate folders visually and save, as one action.
                cands.append({"type": "gtk_save", "text": s, "label": f"save the file in the open file dialog as {s!r}"})
            elif s.startswith("/"):
                # GTK3/other dialogs: a full path goes in the location bar (ctrl+l).
                cands.append({"type": "path", "text": s, "label": f"enter the full path {s!r} in the open file dialog's location bar"})
        cands.append({"type": "done", "label": "the task is complete: stop"})
        # An action already taken 3 times without finishing the task is not offered again.
        return [c for c in cands if repeated[c["label"]] < 3][:MAX_CANDIDATES]

    def decide(self, task: str, history: list[str], shot: bytes | None, cands: list[dict]):
        criteria = {str(i): c["label"] for i, c in enumerate(cands)}
        state = {"task": task, "actions_so_far": history[-12:] or ["(none)"],
                 "window_title": (self.task_window or {}).get("title", "")}
        if shot:
            state["window"] = Image(shot)
        questions = {
            "next": Choice(
                instructions="You are operating the app shown in `window` to accomplish `task`. "
                "`actions_so_far` lists what was already done. Which single action should be taken next?",
                criteria=criteria,
            ),
            "complete": Noul(instructions="Judging by `window` and `window_title` (an editor's title shows the saved "
                             "file name and folder), is `task` already fully accomplished?"),
        }
        try:
            r = self._t("jev", self.jev.system_one, state=state, questions=questions)
        except Exception as e:  # e.g. an image Jev rejects: decide from the element list alone
            print(f"    (jev retry without image: {e!s:.80})", flush=True)
            state.pop("window", None)
            questions["complete"] = Noul(instructions="Judging by `actions_so_far`, is `task` already fully accomplished?")
            r = self._t("jev", self.jev.system_one, state=state, questions=questions)
        ans = r.choices["next"]
        return cands[int(ans.choice)], ans.confidence, r.nouls["complete"].noul

    def act(self, c: dict) -> str:
        if STOP_FILE.exists():
            raise KeyboardInterrupt(f"{STOP_FILE} exists")
        if self.task_window and not self.usable(self.task_window):
            raise BlockedApp(f"refusing to act in {self.task_window.get('title')!r}")
        if c["type"] == "click":
            self._t("act", self.d.click, c["el"], "click", getattr(self, "_els", None))
        elif c["type"] == "key":
            self._t("act", self.d.key, c["key"], self.task_window)
        elif c["type"] == "macro":
            for k in c["keys"]:
                self._t("act", self.d.key, k, self.task_window)
                time.sleep(0.15)
        elif c["type"] == "goto":
            self._t("act", self.d.key, "ctrl+l", self.task_window)
            time.sleep(0.2)
            self._t("act", self.d.type, c["text"], self.task_window)
            self._t("act", self.d.key, "enter", self.task_window)
            time.sleep(1.0)  # let the page start loading
        elif c["type"] == "gtk_save":
            err = self._t("act", self.gtk.save_as, self.task_window, c["text"])
            if err:
                return f"{c['label'][:80]}: FAILED ({err})"
        elif c["type"] == "path":
            self._t("act", self.d.key, "ctrl+l", self.task_window)
            time.sleep(0.2)
            self._t("act", self.d.key, "ctrl+a", self.task_window)
            self._t("act", self.d.type, c["text"], self.task_window)
        elif c["type"] == "type":
            self._t("act", self.d.type, c["text"], self.task_window)
        time.sleep(0.2)
        return c["label"][:120]

    def run(self, task: str) -> dict:
        history: list[str] = []
        repeated: Counter = Counter()
        status = "max_steps"
        need_tree = True
        likely_done = 0
        for step in range(1, self.max_steps + 1):
            els, shot = self.observe(need_tree)
            if self.debug_dir and shot:
                (self.debug_dir / f"{step:03d}.jpg").write_bytes(shot)
            cands = self.candidates(task, els, repeated)
            choice, conf, complete = self.decide(task, history, shot, cands)
            self.on_step({"step": step, "label": choice["label"], "conf": conf, "done": complete,
                          "window": (self.task_window or {}).get("title", ""), "ms": self._step_ms()})
            likely_done = likely_done + 1 if complete >= 0.5 else 0
            if complete >= self.done_threshold or likely_done >= 3:
                status = "done"
                break
            if choice["type"] == "done":
                # "done" picked but the completion check isn't convinced: take the best real action instead.
                cands = [c for c in cands if c["type"] != "done"]
                choice, conf, complete = self.decide(task, history, shot, cands)
                self.on_step({"step": step, "label": f"(not done yet) {choice['label']}", "conf": conf, "done": complete,
                              "window": (self.task_window or {}).get("title", ""), "ms": self._step_ms()})
            repeated[choice["label"]] += 1
            history.append(self.act(choice))
            need_tree = choice["type"] == "click"  # clicks can open menus/dialogs; keys and typing reuse the tree
        timing = {k: {"calls": self.calls[k], "ms": round(v)} for k, v in self.ms.items()}
        return {"status": status, "steps": len(history), "history": history, "timing": timing}
