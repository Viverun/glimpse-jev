"""Save through a GTK4 / portal file dialog to an exact path.

GTK4 dialogs ignore typed paths and report element positions relative to their parent, so folders
can't be clicked from the accessibility tree. Instead: Home via its accessibility action, then for each
folder Jev finds it on the dialog screenshot (Box, then numbered marks for an exact element) and the
real cursor double-clicks it; finally the File Name is set and Save is triggered by its action.
"""

import base64
import io
import time
from pathlib import Path

from PIL import Image as PILImage
from typesafe_sdk import Box, Choice, Image, Noul, TypeSafeClient

from marks import draw_marks, find_elements


class GtkFileDialog:
    def __init__(self, mcp, jev: TypeSafeClient | None = None):
        self.mcp = mcp
        self.jev = jev or __import__('config').jev_client(headers={"TypeSafe-Danger-Zone-Images": "true"})

    def _tree(self, win, max_nodes: int = 400):
        return self.mcp.call("get_app_state", window_id=win["window_id"], include_screenshot=False, max_nodes=max_nodes, max_depth=40)["accessibility_tree"]

    def _shot(self, win):
        st = self.mcp.call("get_app_state", window_id=win["window_id"], include_screenshot=True, format="png", max_nodes=1)
        meta = st["screenshot"]
        img = PILImage.open(io.BytesIO(base64.b64decode(st["_images"][0]))).convert("RGB")
        return img, meta

    def _action(self, win, pred, action=None) -> bool:
        el = next((e for e in self._tree(win) if pred(e)), None)
        if not el:
            return False
        self.mcp.call("perform_action", element_index=el["index"], action=action)
        return True

    def _open_folder(self, win, name: str) -> bool:
        """Find folder `name` on the dialog screenshot and double-click it with the cursor."""
        # Bring it into view first: GTK4 list items expose a scroll-to action.
        # Read deep: in a big folder the target cell is past the first few hundred tree nodes.
        cell = next((e for e in self._tree(win, max_nodes=2000) if e["role"] in ("grid cell", "list item")
                     and (e.get("name") or "").split(". ")[0] == name), None)
        if cell:
            self.mcp.call("perform_action", element_index=cell["index"], action="listitem.scroll-to")
        time.sleep(0.3)
        img, meta = self._shot(win)
        buf = io.BytesIO(); img.save(buf, "JPEG", quality=85); jpeg = buf.getvalue()
        target = f"the folder named '{name}' in the file list"
        r = self.jev.system_one(state={"screen": Image(jpeg)}, questions={
            "visible": Noul(instructions=f"Is {target} visible on `screen`?"),
            "box": Box(instructions=f"{target} on `screen`")})
        if r.nouls["visible"].noul < 0.5:
            return False
        b = r.answers["box"].box
        cx, cy = (b.left + b.right) / 2 * img.width, (b.top + b.bottom) / 2 * img.height
        # Snap to the exact element: numbered marks on a crop around the guess, Jev picks one.
        cw, ch = min(400, img.width), min(300, img.height)
        left = int(min(max(cx - cw / 2, 0), img.width - cw)); top = int(min(max(cy - ch / 2, 0), img.height - ch))
        crop = img.crop((left, top, left + cw, top + ch)); cbuf = io.BytesIO(); crop.save(cbuf, "JPEG", quality=85)
        rects = find_elements(cbuf.getvalue())
        if rects:
            marked = draw_marks(cbuf.getvalue(), rects)
            crit = {str(i): None for i in range(1, len(rects) + 1)} | {"none": "None of the boxes."}
            pick = self.jev.system_one(state={"screen": Image(marked)}, questions={"m": Choice(
                instructions=f"`screen` shows elements outlined in red with numbers. Which number marks {target}?",
                criteria=crit)}).choices["m"]
            if pick.choice != "none" and pick.confidence >= 0.4:
                x, y, w, h = rects[int(pick.choice) - 1]
                cx, cy = left + x + w / 2, top + y + h / 2
        # Image pixels -> window-relative coordinate pixels for the MCP click.
        sx, sy = meta["coordinate_width"] / meta["width"], meta["coordinate_height"] / meta["height"]
        self.mcp.call("click", x=round(cx * sx), y=round(cy * sy), relative=True, window_id=win["window_id"], click_count=2)
        time.sleep(0.6)
        return True

    def save_as(self, win, path: str) -> str | None:
        """Set the name, navigate to `path`'s folder, save. Returns a failure reason, or None.

        The File Name field drops out of the accessibility tree once the dialog navigates, but keeps its
        text, so the name is set first.
        """
        p = Path(path).expanduser()
        if not self._action_set_name(win, p.name):
            return "no File Name field"
        if not self._goto_typed(win, p.parent):
            # Fallback: Home, then find each folder on the screenshot and double-click it.
            if not self._action(win, lambda e: e["role"] == "button" and (e.get("name") or "").startswith("Home")):
                return "no Home button in the dialog"
            time.sleep(0.5)
            for part in p.parent.relative_to(Path.home()).parts:
                if not self._open_folder(win, part):
                    return f"could not find folder '{part}'"
        for attempt in range(3):
            if not (self._action(win, lambda e: e["role"] == "button" and (e.get("name") or "").lower() == "save")
                    or self._action_deep(win, "save")):
                return "no Save button" if attempt == 0 else None
            for _ in range(6):  # saved once the dialog window is gone
                time.sleep(0.35)
                if not any(w.get("window_id") == win["window_id"] for w in self.mcp.call("list_windows").get("windows", [])):
                    return None
        return "the Save dialog stayed open after pressing Save"

    def _goto_typed(self, win, folder: Path) -> bool:
        """Location bar: ctrl+l, type the folder path, delete (drops the autocomplete suggestion GTK
        appends), enter. Confirmed by the breadcrumb showing the folder."""
        wid = win["window_id"]
        try:
            self.mcp.call("press_key", key="ctrl+l", window_id=wid)
            time.sleep(0.3)
            self.mcp.call("type_text", text=str(folder) + "/", window_id=wid)
            time.sleep(0.3)
            self.mcp.call("press_key", key="delete", window_id=wid)
            self.mcp.call("press_key", key="enter", window_id=wid)
        except RuntimeError:
            return False
        for _ in range(6):
            time.sleep(0.4)
            names = [(e.get("name") or "") for e in self._tree(win, max_nodes=2000) if e.get("role") in ("label", "button", "toggle button")]
            if any(n.startswith("Unable to find") for n in names):
                return False
            if folder.name in names:
                return True
        return False

    def _action_set_name(self, win, name: str) -> bool:
        el = None
        for i in range(8):  # a just-opened dialog may not have drawn its name field yet; big folders push it deep
            el = next((e for e in self._tree(win, max_nodes=400 if i < 2 else 2000)
                       if e["role"] == "text box" and e.get("name") == "File Name"), None)
            if el:
                break
            time.sleep(0.3)
        if not el:
            return False
        self.mcp.call("set_value", element_index=el["index"], value=name)
        return True

    def _action_deep(self, win, button: str) -> bool:
        el = next((e for e in self._tree(win, max_nodes=2000) if e["role"] == "button" and (e.get("name") or "").lower() == button), None)
        if not el:
            return False
        self.mcp.call("perform_action", element_index=el["index"])
        return True
