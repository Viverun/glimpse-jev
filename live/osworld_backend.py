"""glimpse on an OSWorld virtual machine: the same desktop interface as the live backend, served by OSWorld's server.

OSWorld runs Ubuntu in a VM and exposes it over HTTP (desktop_env/server/main.py): a screenshot, the accessibility
tree as XML (AT-SPI inside the VM), pyautogui commands, and bash. This module translates that into what glimpse
already uses (list_windows, get_app_state with an element tree and a window screenshot, clicks, keys, typing), so
the same agent runs unchanged. Select it with GLIMPSE_BACKEND=osworld and OSWORLD_URL=http://<vm-ip>:5000.
"""

import base64
import hashlib
import io
import json
import re
import time

import requests

NS = {
    "st": "https://accessibility.ubuntu.example.org/ns/state",
    "cp": "https://accessibility.ubuntu.example.org/ns/component",
    "act": "https://accessibility.ubuntu.example.org/ns/action",
    "val": "https://accessibility.ubuntu.example.org/ns/value",
}
PAIR = re.compile(r"\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\)")


class OSWorldServer:
    def __init__(self, url: str):
        self.url = url.rstrip("/")

    def screenshot(self) -> bytes:
        r = requests.get(self.url + "/screenshot", timeout=20)
        r.raise_for_status()
        return r.content

    def accessibility(self) -> str:
        r = requests.get(self.url + "/accessibility", timeout=30)
        r.raise_for_status()
        return r.json().get("AT", "")

    def pyautogui(self, code: str) -> None:
        cmd = ["python3", "-c", "import pyautogui, time; pyautogui.FAILSAFE = False; " + code]
        requests.post(self.url + "/execute", headers={"Content-Type": "application/json"},
                      data=json.dumps({"command": cmd, "shell": False}), timeout=60)

    def bash(self, script: str, timeout: int = 60) -> tuple[str, int]:
        r = requests.post(self.url + "/run_bash_script", headers={"Content-Type": "application/json"},
                          data=json.dumps({"script": script, "timeout": timeout}), timeout=timeout + 10)
        d = r.json() if r.ok else {}
        return (d.get("output") or "") + (d.get("error") or ""), int(d.get("returncode", 1 if not r.ok else 0))


def parse_tree(xml: str) -> list[dict]:
    """OSWorld's XML -> flat nodes like computer-use-linux's: index, depth, parent_index, role, name, text, states,
    actions, and ABSOLUTE screen bounds (OSWorld gives screen coordinates)."""
    import xml.etree.ElementTree as etree  # the standard library parser handles OSWorld's namespaced attributes
    root = etree.fromstring(xml.encode() if isinstance(xml, str) else xml)
    out: list[dict] = []

    def walk(el, depth: int, parent: int | None) -> None:
        if not isinstance(el.tag, str):
            return
        attrs = el.attrib
        states = [k.split("}")[1] for k, v in attrs.items() if k.startswith("{" + NS["st"]) and v == "true"]
        pos, size = PAIR.search(attrs.get(f"{{{NS['cp']}}}screencoord", "")), PAIR.search(attrs.get(f"{{{NS['cp']}}}size", ""))
        acts = sorted({k.split("}")[1].rsplit("_", 1)[0] for k in attrs if k.startswith("{" + NS["act"])})
        value = attrs.get(f"{{{NS['val']}}}value")
        node = {"index": len(out), "depth": depth, "parent_index": parent, "role": el.tag.replace("-", " "),
                "name": attrs.get("name") or None, "text": {"content": (el.text or "").strip()} if (el.text or "").strip() else None,
                "states": states, "actions": [{"name": a} for a in acts], "value": value,
                "bounds": {"x": int(pos.group(1)), "y": int(pos.group(2)), "width": int(size.group(1)), "height": int(size.group(2))}
                if pos and size else None}
        out.append(node)
        me = node["index"]
        for ch in el:
            walk(ch, depth + 1, me)
    walk(root, 0, None)
    return out


def _wid(app: str, title: str) -> int:
    return int(hashlib.sha1(f"{app}|{title}".encode()).hexdigest()[:8], 16)


class OSWorldMCP:
    """Answers the computer-use-linux tool calls glimpse makes, from the OSWorld server."""

    def __init__(self, server: OSWorldServer):
        self.s = server
        self._nodes: list[dict] = []  # the latest tree read (perform_action / set_value use its indexes)

    def _frames(self, nodes: list[dict]) -> list[tuple[dict, dict]]:
        """(application node, frame node) for each visible top-level window."""
        by = {n["index"]: n for n in nodes}
        out = []
        for n in nodes:
            p = by.get(n["parent_index"])
            if n["role"] in ("frame", "dialog", "window") and p and p["role"] == "application" and n["bounds"] \
                    and n["bounds"]["width"] > 1 and "showing" in n["states"]:
                out.append((p, n))
        return out

    def _windows(self, nodes: list[dict]) -> list[dict]:
        wins = []
        for app, f in self._frames(nodes):
            title = f.get("name") or app.get("name") or ""
            wins.append({"window_id": _wid(app.get("name") or "", title), "title": title,
                         "app_id": f"{(app.get('name') or '').lower()}.desktop", "wm_class": app.get("name"),
                         "pid": _wid(app.get("name") or "", ""), "focused": "active" in f["states"], "bounds": dict(f["bounds"])})
        if wins and not any(w["focused"] for w in wins):
            wins[-1]["focused"] = True
        return wins

    def _subtree(self, nodes: list[dict], root: dict) -> list[dict]:
        keep, out, idx = {root["index"]}, [], {}
        for n in nodes:
            if n["index"] == root["index"] or n["parent_index"] in keep:
                keep.add(n["index"])
                idx[n["index"]] = len(out)
                out.append({**n, "index": len(out), "depth": n["depth"] - root["depth"] + 1,
                            "parent_index": idx.get(n["parent_index"]), "_orig": n["index"]})
        return out

    def call(self, tool: str, **a) -> dict:
        if tool == "list_windows":
            return {"windows": self._windows(parse_tree(self.s.accessibility()))}
        if tool == "get_app_state":
            nodes = parse_tree(self.s.accessibility())
            wins = self._windows(nodes)
            target = next((w for w in wins if w["window_id"] == a.get("window_id")), None) or next((w for w in wins if w["focused"]), None)
            frame = next((f for app, f in self._frames(nodes) if target and _wid(app.get("name") or "", f.get("name") or app.get("name") or "") == target["window_id"]), None)
            tree = self._subtree(nodes, frame)[: a.get("max_nodes") or 600] if frame else []
            self._nodes = tree
            out = {"accessibility_tree": tree}
            if a.get("include_screenshot", True) and target:
                from PIL import Image
                img = Image.open(io.BytesIO(self.s.screenshot())).convert("RGB")
                b = target["bounds"]
                crop = img.crop((max(0, b["x"]), max(0, b["y"]), min(img.width, b["x"] + b["width"]), min(img.height, b["y"] + b["height"])))
                w0, h0 = crop.size
                mw = a.get("max_width") or w0
                if crop.width > mw:
                    crop = crop.resize((mw, round(crop.height * mw / crop.width)))
                buf = io.BytesIO()
                crop.save(buf, "JPEG", quality=a.get("quality") or 70)
                out["_images"] = [base64.b64encode(buf.getvalue()).decode()]
                out["screenshot"] = {"width": crop.width, "height": crop.height, "coordinate_width": w0, "coordinate_height": h0}
            return out
        if tool == "screenshot":
            return {"_images": [base64.b64encode(self.s.screenshot()).decode()]}
        if tool == "activate_window":
            w = next((w for w in self._windows(parse_tree(self.s.accessibility())) if w["window_id"] == a.get("window_id")), None)
            if w:
                title = w["title"].replace("'", "'\\''")
                out, rc = self.s.bash(f"wmctrl -a '{title}' 2>/dev/null || xdotool search --name '{title}' windowactivate 2>/dev/null")
                if rc != 0:  # no window tool in the VM: click its title bar
                    b = w["bounds"]
                    self.s.pyautogui(f"pyautogui.click({b['x'] + b['width'] // 2}, {b['y'] + 8})")
            return {"ok": True}
        if tool in ("perform_action", "set_value"):
            n = next((x for x in self._nodes if x["index"] == a.get("element_index")), None)
            if not n or not n.get("bounds"):
                raise RuntimeError(f"{tool} failed: element {a.get('element_index')} not found")
            b = n["bounds"]
            x, y = b["x"] + b["width"] // 2, b["y"] + b["height"] // 2
            if tool == "perform_action":
                self.s.pyautogui(f"pyautogui.click({x}, {y})")
            else:
                self.s.pyautogui(f"pyautogui.click({x}, {y}); pyautogui.hotkey('ctrl', 'a'); pyautogui.write({str(a.get('value', ''))!r}, interval=0.01)")
            return {"ok": True}
        if tool == "press_key":
            keys = [k.strip().lower() for k in str(a.get("key", "")).replace("Return", "enter").split("+") if k.strip()]
            self.s.pyautogui(f"pyautogui.hotkey(*{keys!r})")
            return {"ok": True}
        if tool == "type_text":
            self.s.pyautogui(f"pyautogui.write({str(a.get('text', ''))!r}, interval=0.01)")
            return {"ok": True}
        if tool == "click":
            self.s.pyautogui(f"pyautogui.click({int(a['x'])}, {int(a['y'])}, clicks={int(a.get('click_count', 1))}, button={a.get('button', 'left')!r})")
            return {"ok": True}
        if tool == "scroll":
            self.s.pyautogui(f"pyautogui.scroll({-3 if a.get('direction', 'down') == 'down' else 3})")
            return {"ok": True}
        raise RuntimeError(f"{tool} is not available on the OSWorld backend")

    def close(self) -> None:
        pass


class Pointer:
    """cursor.py's real-pointer calls, inside the VM (pyautogui moves smoothly with duration)."""

    def __init__(self, server: OSWorldServer):
        self.s = server

    def glide(self, x: float, y: float) -> None:
        self.s.pyautogui(f"pyautogui.moveTo({round(x)}, {round(y)}, duration=0.15)")

    def click(self, x: float, y: float, button: str = "left", count: int = 1) -> None:
        self.s.pyautogui(f"pyautogui.moveTo({round(x)}, {round(y)}, duration=0.15); time.sleep(0.05); "
                         f"pyautogui.click(clicks={count}, button={button!r})")

    def stroke(self, points: list[tuple[float, float]], step: float = 4.0) -> None:
        if not points:
            return
        moves = "; ".join(f"pyautogui.moveTo({round(x)}, {round(y)}, duration=0.03)" for x, y in points[1:])
        self.s.pyautogui(f"pyautogui.moveTo({round(points[0][0])}, {round(points[0][1])}, duration=0.1); pyautogui.mouseDown(); "
                         f"{moves}; pyautogui.mouseUp()")

    def scroll(self, clicks: int, at: tuple[float, float] | None = None) -> None:
        where = f", x={round(at[0])}, y={round(at[1])}" if at else ""
        self.s.pyautogui(f"pyautogui.scroll({-clicks}{where})")


def make_desktop(url: str):
    """A glimpse Desktop whose screen, pointer, keyboard, apps and shell are the OSWorld VM's."""
    import cursor
    from a11y_agent import Desktop, _text
    server = OSWorldServer(url)
    mcp = OSWorldMCP(server)
    pointer = Pointer(server)
    cursor.glide, cursor.click, cursor.stroke, cursor.scroll = pointer.glide, pointer.click, pointer.stroke, pointer.scroll

    class OSWorldDesktop(Desktop):
        def __init__(self):  # no local MCP server: everything goes to the VM
            self.mcp = mcp
            self.server = server
            self.last_text = ""

        def windows(self):
            return mcp.call("list_windows")["windows"]

        def elements(self, pid, state=None, wins=None):
            state = state or mcp.call("get_app_state", include_screenshot=False)
            tree = state.get("accessibility_tree", [])
            chunks = []
            for e in tree:
                t = (_text(e) or "").strip()
                if t and t not in chunks:
                    chunks.append(t)
            self.last_text = "\n".join(chunks)[-8000:]
            out = []
            for e in tree:
                b = e.get("bounds")
                if not b or b["width"] <= 1 or b["height"] <= 1 or "showing" not in (e.get("states") or []):
                    continue
                if not (e.get("name") or _text(e) or e.get("value")):
                    continue
                out.append({**e, "_pid": pid, "abs": dict(b), "window": tree[0].get("name") if tree else ""})
            return out[:400]

        def click(self, element, kind="click", siblings=None):
            b = element["abs"]
            x, y = b["x"] + b["width"] / 2, b["y"] + b["height"] / 2
            pointer.click(x, y, "right" if kind == "right_click" else "left", 2 if kind == "double_click" else 1)
            return round(x), round(y)

        def type(self, text, window):
            mcp.call("type_text", text=text)

        def key(self, combo, window):
            mcp.call("press_key", key=combo)
            time.sleep(0.1)

        def launch(self, cmd: list[str], env: dict | None = None) -> None:
            import shlex
            server.bash(f"nohup {' '.join(shlex.quote(c) for c in cmd)} >/dev/null 2>&1 &", timeout=10)

        def shell(self, cmd: str, timeout: float = 60) -> tuple[str, int]:
            return server.bash(cmd, int(timeout))

    return OSWorldDesktop()
