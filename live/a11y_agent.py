"""Jev + GPT-6-luna agent on the live desktop through computer-use-linux, without screenshots.

See:   the focused app's AT-SPI accessibility tree, as text (computer-use-linux get_app_state).
Find:  Jev picks the target element from that list with one Choice question.
Act:   a real cursor click at the element's exact center; keys and text go to a targeted app window,
       which computer-use-linux refuses if it cannot verify focus. Terminals are never targeted.
Check: Jev answers whether the new element list shows the subgoal's expected outcome.
Plan:  GPT-6-luna plans subgoals from the element list (a screenshot only with --screenshots).
"""

import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

from openai import OpenAI
from typesafe_sdk import Choice, Noul, TypeSafeClient

sys.path.insert(0, str(Path(__file__).parent))
from desktop_mcp import DesktopMCP  # noqa: E402

LUNA_ENDPOINT = __import__("config").setting("planner_base_url")
LUNA_DEPLOYMENT = __import__("config").setting("planner_model")
CLICK_SCALE = 4 / 3  # AT-SPI bounds are in GNOME logical px (1440 wide); MCP click takes 1920-wide coordinates
STOP_FILE = Path.home() / "glimse-demo" / "STOP"
FAST_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 \n\t`~!@#$%^&*()-_=+[]{}\\|;:'\",.<>/?")
YDOTOOL_ENV = {**os.environ, "YDOTOOL_SOCKET": f"/run/user/{os.getuid()}/.ydotool_socket"}
TERMINAL_CLASSES = ("terminal", "konsole", "kitty", "alacritty", "wezterm", "ghostty")
BLOCKED_KEYS = {"ctrl+alt+t", "alt+f2"}
INTERACTIVE = {
    "push button", "button", "toggle button", "check box", "radio button", "menu item", "check menu item",
    "radio menu item", "menu", "combo box", "text", "entry", "password text", "spin button", "list item",
    "page tab", "table cell", "tree item", "link", "icon", "slider", "label", "heading", "paragraph",
    "text box", "grid cell", "row", "cell", "switch", "toggle", "search box", "tab",
}
MAX_ELEMENTS = 200


class Timer:
    def __init__(self):
        self.ms, self.calls = defaultdict(float), defaultdict(int)

    def __call__(self, name, fn, *a, **k):
        t = time.perf_counter()
        try:
            return fn(*a, **k)
        finally:
            self.ms[name] += (time.perf_counter() - t) * 1000
            self.calls[name] += 1

    def summary(self):
        return {k: {"calls": self.calls[k], "ms": round(v)} for k, v in self.ms.items()}


class Desktop:
    """What the agent can see and do, through computer-use-linux."""

    def __init__(self):
        self.mcp = DesktopMCP()

    def focused_window(self) -> dict | None:
        wins = self.mcp.call("list_windows").get("windows", [])
        return next((w for w in wins if w.get("focused")), None)

    def windows(self) -> list[dict]:
        return self.mcp.call("list_windows").get("windows", [])

    def elements(self, pid: int, state: dict | None = None, wins: list[dict] | None = None) -> list[dict]:
        """Visible elements of an app, with screen-absolute bounds in GNOME logical pixels.

        AT-SPI bounds on Wayland are relative to each top-level window, measured from the GTK surface
        (which includes the client-side shadow). Each element is mapped to its top-level node, that node
        to a compositor window (title, then size), and the shadow inset is subtracted.
        """
        if state is None:
            state = self.mcp.call("get_app_state", pid=pid, include_screenshot=False, max_nodes=1500, max_depth=40)
        tree = state.get("accessibility_tree", [])
        by_index = {e["index"]: e for e in tree}
        # Readable text in the window (answers, terminal output, page text), for the planner to read.
        chunks, seen = [], set()
        for e in tree:
            t = _text(e) or (e.get("name") if e.get("role") in ("label", "heading", "paragraph", "static", "terminal", "document text") else "")
            t = (t or "").strip()
            if t and t not in seen and len(t) > 1:
                seen.add(t)
                chunks.append(t)
        self.last_text = "\n".join(chunks)[-8000:]
        wins = [w for w in (wins if wins is not None else self.windows()) if w.get("pid") == pid and w.get("bounds")]
        origin: dict[int, tuple[float, float]] = {}
        for top in (e for e in tree if e.get("depth") == 1 and e.get("bounds")):
            tb = top["bounds"]
            def fit(w):
                wb = w["bounds"]
                dw, dh = tb["width"] - wb["width"], tb["height"] - wb["height"]
                if dw < 0 or dh < 0 or dw > 120 or dh > 120:
                    return None
                return (0 if (w.get("title") or "") == (top.get("name") or "") else 1, dw + dh)
            scored = [(fit(w), w) for w in wins]
            scored = [x for x in scored if x[0] is not None]
            if scored:
                w = min(scored, key=lambda x: x[0])[1]
                wb = w["bounds"]
                origin[top["index"]] = (wb["x"] - (tb["width"] - wb["width"]) / 2, wb["y"] - (tb["height"] - wb["height"]) / 2)
        out = []
        for e in tree:
            b = e.get("bounds")
            if not b or b["width"] <= 1 or b["height"] <= 1 or e.get("role") not in INTERACTIVE:
                continue
            states = e.get("states") or []
            if states and "showing" not in states and "visible" not in states:
                continue
            if not (e.get("name") or _text(e) or e.get("value") or e.get("description") or e.get("role") in ("text", "entry")):
                continue
            top = e
            while top.get("depth", 0) > 1 and top.get("parent_index") in by_index:
                top = by_index[top["parent_index"]]
            if top["index"] not in origin:
                continue
            ox, oy = origin[top["index"]]
            e = {**e, "_pid": pid, "abs": {"x": round(ox + b["x"]), "y": round(oy + b["y"]), "width": b["width"], "height": b["height"]},
                 "window": top.get("name") or top.get("role")}
            out.append(e)
        return out[:MAX_ELEMENTS]

    def click(self, element: dict, kind: str = "click", siblings: list[dict] | None = None) -> tuple[int, int]:
        """Move the real cursor to the element; activate it with its AT-SPI action when it has one.

        LibreOffice ignores virtual-pointer clicks but responds to accessibility actions. An action by
        role+name hits the FIRST match, so it is only used when that name is unique among `siblings`;
        otherwise the element is re-located in a fresh tree read (indexes change on every read) and
        triggered by its new index. Without an action: a real pointer click.
        """
        b = element["abs"]
        cx, cy = b["x"] + b["width"] / 2, b["y"] + b["height"] / 2
        x, y = round(cx * CLICK_SCALE), round(cy * CLICK_SCALE)
        subprocess.run(["ydotool", "mousemove", "--absolute", "-x", str(round(cx)), "-y", str(round(cy))],
                       env=YDOTOOL_ENV, capture_output=True)  # visible cursor move, GNOME logical px
        names = [a.get("name", "").lower() for a in element.get("actions") or []]
        primary = next((i for i, n in enumerate(names) if n in ("click", "press", "activate", "toggle", "jump", "open")), None)
        if primary is None and names and not any(names) and element.get("role") in ("button", "push button", "toggle button"):
            # LibreOffice toolbar buttons (Sort Descending, Bold...) have one unnamed action that works, and
            # LibreOffice ignores pointer clicks. (Unnamed actions on menu items are ghosts: never triggered.)
            primary = 0
        if kind == "click" and primary is not None:
            action = element["actions"][primary]["name"] or None
            same = [e for e in (siblings or []) if e.get("role") == element.get("role") and e.get("name") == element.get("name")]
            if element.get("name") and len(same) <= 1:
                try:
                    self.mcp.call("perform_action", role=element["role"], name=element["name"], action=action)
                    return x, y
                except RuntimeError:
                    pass
            fresh = self._relocate(element)
            if fresh is not None:
                self.mcp.call("perform_action", element_index=fresh["index"], action=action)
                return x, y
        args = {"x": x, "y": y}
        if kind == "double_click":
            args["click_count"] = 2
        elif kind == "right_click":
            args["button"] = "right"
        self.mcp.call("click", **args)
        return x, y

    def _relocate(self, element: dict) -> dict | None:
        """Same element in a fresh tree read: same role and name, nearest position."""
        try:
            now = self.elements(element.get("_pid"))
        except Exception:
            return None
        cands = [e for e in now if e.get("role") == element.get("role") and e.get("name") == element.get("name")]
        if not cands:
            return None
        b = element["abs"]
        return min(cands, key=lambda e: abs(e["abs"]["x"] - b["x"]) + abs(e["abs"]["y"] - b["y"]))

    def type(self, text: str, window: dict) -> None:
        """Fast path: ydotool at 1 ms/key (~460 chars/s, exact in tests) once the target window is verified
        focused; it only knows US-keyboard characters. Anything else, or unverified focus: the MCP's
        layout-safe type_text (~24 chars/s), which also verifies focus itself."""
        if text and all(c in FAST_CHARS for c in text) and self._ensure_focus(window):
            r = subprocess.run(["ydotool", "type", "--key-delay", "1", "--key-hold", "1", "--", text],
                               env=YDOTOOL_ENV, capture_output=True)
            if r.returncode == 0:
                return
        self.mcp.call("type_text", text=text, window_id=window["window_id"])

    def _ensure_focus(self, window: dict) -> bool:
        for attempt in range(2):
            cur = next((w for w in self.windows() if w.get("focused")), None)
            if cur and cur.get("window_id") == window.get("window_id"):
                return True
            if attempt == 0:
                try:
                    self.mcp.call("activate_window", window_id=window["window_id"])
                    time.sleep(0.25)
                except Exception:
                    return False
        return False

    def key(self, combo: str, window: dict | None) -> None:
        self.mcp.call("press_key", key=combo, window_id=window["window_id"] if window else None)

    def screenshot_jpeg(self, pid: int) -> str | None:
        state = self.mcp.call("get_app_state", pid=pid, include_screenshot=True, format="jpeg", quality=70, max_width=1280, max_nodes=1)
        shot = state.get("screenshot") or {}
        return shot.get("data") or shot.get("base64")


def _text(e: dict) -> str:
    t = e.get("text")
    if isinstance(t, dict):
        t = t.get("content")
    return t or ""


def describe(e: dict) -> str:
    b = e.get("abs") or e["bounds"]
    bits = [e["role"], repr(e.get("name") or "")]
    text = _text(e)
    if text and text != e.get("name"):
        bits.append(f"text={text[:60]!r}")
    if e.get("value") not in (None, "", e.get("name")):
        bits.append(f"value={str(e.get('value'))[:40]!r}")
    states = [s for s in (e.get("states") or []) if s in ("focused", "checked", "selected", "pressed", "expanded", "editable")]
    if states:
        bits.append("[" + ",".join(states) + "]")
    if e.get("window"):
        bits.append(f"in {e['window']!r}")
    bits.append(f"at ({b['x']},{b['y']}) {b['width']}x{b['height']}")
    return " ".join(bits)


def is_terminal(w: dict | None) -> bool:
    return bool(w) and any(t in (w.get("wm_class") or "").lower() or t in (w.get("app_id") or "").lower() for t in TERMINAL_CLASSES)


PROMPT = """You operate a live Ubuntu GNOME desktop through its accessibility tree (no screenshots).
Each turn you get the open windows and the focused app's visible elements (role, name, text, position).
Reply with JSON only:
{"status": "continue" | "done" | "infeasible",
 "plan": [{"subgoal": "...", "expect": "what the element list shows once this subgoal succeeded",
           "actions": [ACTION, ...]}]}
Plan 1-4 subgoals ahead when confident, one when unsure. ACTION is one of:
  {"type": "click" | "double_click" | "right_click", "target": "the element, by role and name as listed"}
  {"type": "type", "text": "..."}          (into the focused element of the task app)
  {"type": "press", "key": "enter"}        (or "ctrl+s", "escape", "tab", "ctrl+a", ...)
  {"type": "focus_window", "title": "substring of a window title from the list"}
  {"type": "open_app", "name": "Text Editor"}
  {"type": "wait", "seconds": 1}
Keyboard input always goes to the task app, never to a terminal. Never act on terminal windows; ignore them.
Prefer keyboard shortcuts and typing into fields (e.g. the Name Box, file-name entries) over many clicks.
To confirm a dialog prefer pressing enter; escape cancels. Use full absolute file paths exactly as given.
History lines come from an automatic checker that can be wrong. Use "done" only when the task is complete."""


class Agent:
    def __init__(self, desktop: Desktop, use_screenshots: bool = False, max_luna_calls: int = 25):
        self.d, self.use_screenshots, self.max_luna_calls = desktop, use_screenshots, max_luna_calls
        self.jev = __import__('config').jev_client()
        self.luna = OpenAI(base_url=LUNA_ENDPOINT, api_key=__import__("config").setting("planner_api_key"))
        self.timer = Timer()
        self.task_window: dict | None = None

    # --- observation -------------------------------------------------------------------------
    def observe(self) -> tuple[list[dict], list[dict], dict | None]:
        wins = self.timer("windows", self.d.windows)
        focused = next((w for w in wins if w.get("focused")), None)
        if focused and not is_terminal(focused):
            self.task_window = focused
        target = self.task_window
        els = self.timer("a11y_tree", self.d.elements, target["pid"]) if target else []
        return wins, els, target

    def state_text(self, wins, els, target) -> str:
        lines = ["Open windows:"]
        for w in wins:
            if is_terminal(w):
                continue
            mark = " (task app)" if target and w["window_id"] == target["window_id"] else ""
            lines.append(f"- {w.get('title') or '(untitled)'} [{w.get('wm_class')}]{mark}")
        lines.append(f"\nVisible elements of {target.get('title') if target else 'no app'}:")
        lines += [f"- {describe(e)}" for e in els]
        return "\n".join(lines)

    # --- Jev ----------------------------------------------------------------------------------
    def pick(self, target_desc: str, els: list[dict]) -> dict | None:
        if not els:
            return None
        criteria = {str(i): describe(e) for i, e in enumerate(els[:254])}
        criteria["none"] = "None of the listed elements matches."
        r = self.timer("jev_pick", self.jev.system_one,
                       state={"elements": criteria},
                       questions={"el": Choice(instructions=f"Which element in `elements` is: {target_desc}?", criteria=criteria)})
        a = r.choices["el"]
        if a.choice == "none" or a.confidence < 0.3:
            return None
        return els[int(a.choice)]

    def check(self, expect: str, state: str) -> float:
        r = self.timer("jev_check", self.jev.system_one, state={"desktop": state},
                       questions={"met": Noul(instructions=f"Based on `desktop`, is this true: {expect}?")})
        return r.nouls["met"].noul

    # --- Luna ---------------------------------------------------------------------------------
    def plan(self, task: str, history: list[str], state: str, pid: int | None) -> dict:
        content = [{"type": "text", "text": f"Task: {task}\n\nHistory:\n" + ("\n".join(history[-8:]) or "(none)") + f"\n\n{state}"}]
        if self.use_screenshots and pid:
            img = self.timer("screenshot", self.d.screenshot_jpeg, pid)
            if img:
                content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img}"}})
        r = self.timer("luna", self.luna.chat.completions.create, model=LUNA_DEPLOYMENT, response_format={"type": "json_object"},
                       messages=[{"role": "system", "content": PROMPT}, {"role": "user", "content": content}])
        return json.loads(r.choices[0].message.content)

    # --- acting -------------------------------------------------------------------------------
    def do(self, action: dict, els: list[dict], wins: list[dict]) -> str | None:
        """Run one action. Returns a failure reason, or None."""
        if STOP_FILE.exists():
            raise KeyboardInterrupt(f"{STOP_FILE} exists")
        kind = action.get("type")
        if kind in ("click", "double_click", "right_click"):
            el = self.pick(action.get("target", ""), els)
            if el is None:
                return f"no element matches '{action.get('target')}'"
            xy = self.timer("act", self.d.click, el, kind)
            print(f"      {kind} {describe(el)[:80]} -> {xy}", flush=True)
        elif kind == "type":
            if not self.task_window:
                return "no task app window to type into"
            print(f"      type {action.get('text', '')[:60]!r} -> {self.task_window.get('title', '')[:40]}", flush=True)
            self.timer("act", self.d.type, action.get("text", ""), self.task_window)
        elif kind in ("press", "hotkey"):
            combo = action.get("key") or "+".join(action.get("keys", []))
            if combo.lower().replace(" ", "") in BLOCKED_KEYS:
                return f"key {combo} is not allowed"
            print(f"      key {combo} -> {(self.task_window or {}).get('title', '')[:40]}", flush=True)
            self.timer("act", self.d.key, combo, self.task_window)
        elif kind == "focus_window":
            w = next((w for w in wins if action.get("title", "").lower() in (w.get("title") or "").lower() and not is_terminal(w)), None)
            if not w:
                return f"no window titled like '{action.get('title')}'"
            self.timer("act", self.d.mcp.call, "activate_window", window_id=w["window_id"])
            self.task_window = w
        elif kind == "open_app":
            print(f"      open app {action.get('name')}", flush=True)
            self.timer("act", self.d.key, "super", None)
            time.sleep(0.6)
            self.timer("act", self.d.mcp.call, "type_text", text=action.get("name", ""))
            time.sleep(0.6)
            self.timer("act", self.d.key, "enter", None)
            time.sleep(2.0)
            self.task_window = None  # the new app becomes the task app once focused
        elif kind == "wait":
            time.sleep(min(float(action.get("seconds", 1)), 5))
        else:
            return f"unknown action {kind}"
        time.sleep(0.15)
        return None

    def run(self, task: str) -> dict:
        history: list[str] = []

        def log(line: str):
            print("   " + line, flush=True)
            history.append(line)

        status, calls, fails = "continue", 0, 0
        while calls < self.max_luna_calls:
            wins, els, target = self.observe()
            calls += 1
            try:
                reply = self.plan(task, history, self.state_text(wins, els, target), target and target["pid"])
            except Exception as e:
                log(f"[planner error: {e!s:.150}]")
                continue
            status = reply.get("status", "continue")
            print(f"  luna #{calls}: {status}, {len(reply.get('plan', []))} subgoal(s)", flush=True)
            if status in ("done", "infeasible"):
                break
            for sub in reply.get("plan", [])[:4]:
                failure = None
                for action in sub.get("actions", []):
                    failure = self.do(action, els, wins)
                    if failure:
                        break
                    if action.get("type") in ("click", "double_click", "right_click", "focus_window", "open_app", "press", "hotkey"):
                        wins, els, target = self.observe()  # the UI may have changed; refresh before the next target
                if failure:
                    fails += 1
                    log(f"- {sub.get('subgoal')}: STOPPED, {failure}")
                    break
                time.sleep(0.3)
                wins, els, target = self.observe()
                p = self.check(sub.get("expect", ""), self.state_text(wins, els, target))
                if p < 0.5:
                    fails += 1
                    log(f"- {sub.get('subgoal')}: checker says NOT met (P={p:.2f}, expected '{sub.get('expect')}'); verify in the element list")
                    if fails >= 2:
                        log("[repeated failures: if the state shows success, move on; otherwise use a different method]")
                    break
                fails = 0
                log(f"- {sub.get('subgoal')}: ok")
        return {"status": status, "luna_calls": calls, "history": history, "timing": self.timer.summary()}
