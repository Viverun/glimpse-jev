"""Save through LibreOffice's own (GTK3) Save dialog to an exact path, in the right format.

The dialog runs inside LibreOffice ("Save", wm_class soffice): its name field takes a full path, and its
file-type combo lists the formats as menu items. The format is chosen from the file extension, the path
is set, enter saves, a "keep current format?" question is answered, and the file is checked on disk.
"""

import os
import time
from pathlib import Path

FORMATS = {
    ".ods": "ODF Spreadsheet (.ods)", ".xlsx": "Excel 2007-365 (.xlsx)", ".xls": "Excel 97-2003 (.xls)",
    ".csv": "Text CSV (.csv)", ".odt": "ODF Text Document (.odt)", ".docx": "Word 2007-365 (.docx)",
    ".doc": "Word 97-2003 (.doc)", ".rtf": "Rich Text (.rtf)", ".txt": "Text (.txt)",
    ".odp": "ODF Presentation (.odp)", ".pptx": "PowerPoint 2007-365 (.pptx)",
}
ODF = (".ods", ".odt", ".odp", ".odg")
DIALOGS = ("Save", "Save As", "Export")  # titles of LibreOffice's own file dialogs


def is_libreoffice(w: dict | None) -> bool:
    w = w or {}
    return w.get("wm_class") in ("soffice", "libreoffice-calc", "libreoffice-writer", "libreoffice-impress") \
        or "libreoffice" in (w.get("app_id") or "")


class LibreOfficeSave:
    def __init__(self, desktop):
        self.d = desktop

    def _focused(self):
        return next((w for w in self.d.windows() if w.get("focused")), None)

    def _tree(self, w, n=2000):
        return self.d.mcp.call("get_app_state", window_id=w["window_id"], include_screenshot=False,
                               max_nodes=n, max_depth=40).get("accessibility_tree", [])

    def save_as(self, window: dict, path: str) -> str | None:
        p = Path(os.path.expanduser(path))
        before = p.stat().st_mtime if p.exists() else None
        dlg = self._focused()
        pdf = p.suffix.lower() == ".pdf"  # not a save format: File > Export As > Export Directly as PDF
        if not (dlg and is_libreoffice(dlg) and (dlg.get("title") or "") in DIALOGS):
            # The document being worked on: the given window if it is a document frame, else the focused frame.
            # Never "the first LibreOffice window": with several documents open that saves the wrong one.
            wins = self.d.windows()
            frames = [w for w in wins if is_libreoffice(w) and " — LibreOffice" in (w.get("title") or "")]
            frame = next((w for w in frames if w["window_id"] == window.get("window_id")), None) or \
                next((w for w in frames if w.get("focused")), None) or window
            self.d.mcp.call("activate_window", window_id=frame["window_id"])
            time.sleep(0.3)
            def dialog():
                f = self._focused()
                return f if f and is_libreoffice(f) and (f.get("title") or "") in DIALOGS else None

            dlg = None
            if not pdf:  # the shortcut is instant; a full menu read of a big sheet takes ~4 s
                self.d.key("ctrl+shift+s", frame)
                for _ in range(8):
                    time.sleep(0.3)
                    if dlg := dialog():
                        break
            if not dlg:
                # The menu item's own action: in the tree even with the menu closed. Writer also exposes a ghost
                # copy of each menu item with a blank action (triggering one closed LibreOffice): only "Click".
                want_item = "Export Directly as PDF" if pdf else "Save As..."
                item = next((e for e in self._tree(frame, 3000) if e.get("role") == "menu item"
                             and (e.get("name") or "").replace("…", "...") == want_item
                             and any((x.get("name") or "").lower() == "click" for x in e.get("actions") or [])), None)
                if not item:
                    return f"no '{want_item}' menu item"
                self.d.mcp.call("perform_action", element_index=item["index"], action="Click")
                for _ in range(20):
                    time.sleep(0.4)
                    if dlg := dialog():
                        break
                else:
                    return f"the {'Export' if pdf else 'Save'} dialog did not open"
        tree = self._tree(dlg, 150)  # small: the name field is near the top; a 2000-node read cost ~4-5 s
        # File type from the extension (menu items of the type combo); keep whatever is set if unknown.
        want = FORMATS.get(p.suffix.lower())
        if want:
            item = next((e for e in tree if e.get("role") in ("menu item", "radio menu item", "list item")
                         and (e.get("name") or "").startswith(want)
                         and any((x.get("name") or "") for x in e.get("actions") or [])), None)
            if item:
                self.d.mcp.call("perform_action", element_index=item["index"])
                time.sleep(0.3)
                tree = self._tree(dlg, 150)  # small: the name field is near the top; a 2000-node read cost ~4-5 s
        field = next((e for e in tree if e.get("role") in ("text", "text box", "entry")
                      and "editable" in (e.get("states") or []) and "focused" in (e.get("states") or [])), None) \
            or next((e for e in tree if e.get("role") in ("text", "text box", "entry") and "editable" in (e.get("states") or [])), None)
        if not field:
            return "no file name field in the Save dialog"
        self.d.mcp.call("set_value", element_index=field["index"], value=str(p))
        time.sleep(0.2)
        self.d.key("enter", dlg)
        # Possible follow-ups: "file exists, replace?" and "keep current format?".
        for _ in range(40):
            time.sleep(0.2)
            if p.exists() and p.stat().st_mtime != before and (self._focused() or {}).get("title") not in DIALOGS:
                return None
            f = self._focused()
            if not f or not is_libreoffice(f) or " — LibreOffice" in (f.get("title") or ""):
                # Only a question dialog needs reading; the document window's tree costs ~1 s per read.
                continue
            names = {(e.get("name") or "").replace("’", "'"): e for e in self._tree(f, 200)
                     if e.get("role") in ("button", "push button")}
            pick = None
            if "Replace" in names:
                pick = names["Replace"]
            elif "Yes" in names and "No" in names:  # "file already exists, overwrite?"
                pick = names["Yes"]
            elif "Use ODF Format" in names or "Use ODF Format!" in names:
                keep = next((n for n in names if n.startswith("Use ") and "ODF" not in n), None)
                pick = names.get("Use ODF Format") or names.get("Use ODF Format!") if p.suffix.lower() in ODF or not keep else names[keep]
            if pick:
                self.d.mcp.call("perform_action", element_index=pick["index"])
        return None if p.exists() and p.stat().st_mtime != before else f"{p} was not written"
