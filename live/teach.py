"""Teach jev by demonstration: run a skill's steps with jev's own executor on the live desktop, verify the result
on disk, and only then store it as a taught lesson and a reusable workflow with {variables}.

  uv run python live/teach.py                 # every skill
  uv run python live/teach.py calc_total_row  # some
Works in ~/glimse-demo/teach. Hands off the desktop while it runs (touching input stops it).
"""

import csv
import re
import sys
import time
from pathlib import Path

import memory
from a11y_agent import Desktop
from hybrid_agent import HybridAgent, run_shell, step_label
from jev_cli import installed_apps
from livevm import flat_pointer
from takeover import Takeover

DIR = Path.home() / "glimse-demo" / "teach"
ROWS = [("Notebook", 12, 3.5), ("Pen", 40, 1.25), ("Backpack", 3, 45.0), ("Stapler", 5, 12.0), ("Mug", 8, 9.5)]


def sales_csv():
    with open(DIR / "sales.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Product", "Quantity", "Price"])
        w.writerows(ROWS)


PY = "python3 -c"
SKILLS = {
    "calc_total_row": dict(
        app="libreoffice-calc", name="Add a total row under a Calc column",
        when="sum a spreadsheet column and put the total under the data",
        variables={"CSV": "the file", "TOTALROW": "first empty row (data rows + 2)", "COL": "column to sum",
                   "LAST": "last data row", "OUT": "where to save"},
        values={"CSV": str(DIR / "sales.csv"), "TOTALROW": "7", "COL": "B", "LAST": "6", "OUT": str(DIR / "total.xlsx")},
        setup=sales_csv,
        steps=[{"do": "open_file", "path": "{CSV}", "app": "LibreOffice Calc"},
               {"do": "select", "range": "A{TOTALROW}"}, {"do": "type", "text": "Total"}, {"do": "key", "key": "tab"},
               {"do": "type", "text": "=SUM({COL}2:{COL}{LAST})"}, {"do": "key", "key": "enter"},
               {"do": "save_as", "path": "{OUT}"}],
        verify=f"{PY} \"import pandas as pd; d=pd.read_excel('{{OUT}}'); r=d.iloc[-1]; assert r.iloc[0]=='Total' and r.iloc[1]==68, d\"",
        lesson="Total row (verified {secs} s): select A{TOTALROW} (TOTALROW = data rows + 2), type Total, key tab, "
               "type =SUM({COL}2:{COL}{LAST}), key enter. Then one save_as step."),
    "calc_currency": dict(
        app="libreoffice-calc", name="Format a Calc column as currency",
        when="show prices or amounts as money",
        variables={"CSV": "the file", "COL": "the money column", "LAST": "last data row", "OUT": "where to save"},
        values={"CSV": str(DIR / "sales.csv"), "COL": "C", "LAST": "6", "OUT": str(DIR / "money.xlsx")},
        setup=sales_csv,
        steps=[{"do": "open_file", "path": "{CSV}", "app": "LibreOffice Calc"},
               {"do": "select", "range": "{COL}2:{COL}{LAST}"}, {"do": "key", "key": "ctrl+shift+4"},
               {"do": "save_as", "path": "{OUT}"}],
        verify=f"{PY} \"import openpyxl; ws=openpyxl.load_workbook('{{OUT}}').active; f=ws['{{COL}}2'].number_format; assert '$' in f, f\"",
        lesson="Currency format (verified {secs} s): select {COL}2:{COL}{LAST}, key ctrl+shift+4. Check: openpyxl "
               "number_format of {COL}2 contains $."),
    "calc_autofilter": dict(
        app="libreoffice-calc", name="Turn on AutoFilter for a Calc table",
        when="add filter dropdowns to a table's header row",
        variables={"CSV": "the file", "LASTCOL": "last column", "LAST": "last data row", "OUT": "where to save"},
        values={"CSV": str(DIR / "sales.csv"), "LASTCOL": "C", "LAST": "6", "OUT": str(DIR / "filter.xlsx")},
        setup=sales_csv,
        steps=[{"do": "open_file", "path": "{CSV}", "app": "LibreOffice Calc"},
               {"do": "select", "range": "A1:{LASTCOL}{LAST}"}, {"do": "key", "key": "ctrl+shift+l"},
               {"do": "save_as", "path": "{OUT}"}],
        verify=f"{PY} \"import openpyxl; ws=openpyxl.load_workbook('{{OUT}}').active; assert ws.auto_filter.ref, 'no filter'\"",
        lesson="AutoFilter (verified {secs} s): select A1:{LASTCOL}{LAST}, key ctrl+shift+l."),
    "writer_pdf": dict(
        app="libreoffice-writer", name="Write a Writer document and export it as PDF",
        when="create a document and save it as a PDF",
        variables={"TEXT": "the document text", "OUT": "the .pdf path"},
        values={"TEXT": "Meeting notes for Monday.", "OUT": str(DIR / "notes.pdf")},
        steps=[{"do": "open_app", "name": "LibreOffice Writer"}, {"do": "type", "text": "{TEXT}"},
               {"do": "save_as", "path": "{OUT}"}],
        verify="head -c 5 '{OUT}' | grep -q '%PDF-'",
        lesson="PDF (verified {secs} s): type the text, then one save_as step with the .pdf path (it uses Export "
               "Directly as PDF)."),
    "writer_bullets": dict(
        app="libreoffice-writer", name="Make a bulleted list in Writer",
        when="write a list of items as bullet points",
        variables={"ITEMS": "items separated by newlines", "OUT": "where to save (.docx)"},
        values={"ITEMS": "Milk\nEggs\nBread", "OUT": str(DIR / "list.docx")},
        steps=[{"do": "open_app", "name": "LibreOffice Writer"}, {"do": "key", "key": "shift+F12"},
               {"do": "type", "text": "{ITEMS}"}, {"do": "save_as", "path": "{OUT}"}],
        verify="test \"$(unzip -p '{OUT}' word/document.xml | grep -o '<w:numPr>' | wc -l)\" -ge 3",
        lesson="Bulleted list (verified {secs} s): key shift+F12 (bullets on), type the items separated by newlines, "
               "then one save_as step. (F12 = numbered list.)"),
    "editor_replace": dict(
        app="org.gnome.TextEditor", name="Find and replace text in Text Editor",
        when="replace every occurrence of a word in a text file",
        variables={"FILE": "the text file", "FIND": "text to find", "REPLACE": "replacement"},
        values={"FILE": str(DIR / "colors.txt"), "FIND": "red", "REPLACE": "blue"},
        setup=lambda: (DIR / "colors.txt").write_text("The red car.\nA red door.\n"),
        steps=[{"do": "open_file", "path": "{FILE}", "app": "Text Editor"}, {"do": "key", "key": "ctrl+h"},
               {"do": "type", "text": "{FIND}"}, {"do": "key", "key": "tab"}, {"do": "type", "text": "{REPLACE}"},
               {"do": "click", "target": "Replace All button"}, {"do": "key", "key": "escape"}, {"do": "key", "key": "ctrl+s"}],
        verify="grep -c blue '{FILE}' | grep -qx 2 && ! grep -q red '{FILE}'",
        lesson="Find & replace (verified {secs} s): open_file, key ctrl+h, type the search text, key tab, type the "
               "replacement, click the Replace All button, key escape, key ctrl+s."),
    "files_rename": dict(
        app="org.gnome.Nautilus", name="Rename a file in Files",
        when="rename a file or folder",
        variables={"FOLDER": "folder holding it", "OLD": "current name", "NEW": "new name"},
        values={"FOLDER": str(DIR), "OLD": "draft.txt", "NEW": "final.txt"},
        setup=lambda: (DIR / "draft.txt").write_text("x\n"),
        steps=[{"do": "open_file", "path": "{FOLDER}", "app": "Files"}, {"do": "key", "key": "ctrl+s"},
               {"do": "type", "text": "{OLD}"}, {"do": "key", "key": "enter"}, {"do": "key", "key": "F2"},
               {"do": "key", "key": "ctrl+a"}, {"do": "type", "text": "{NEW}"}, {"do": "key", "key": "enter"}],
        verify="test -f '{FOLDER}/{NEW}' && ! test -e '{FOLDER}/{OLD}'",
        lesson="Rename (verified {secs} s): open_file the folder in Files, key ctrl+s (select matching), type the exact "
               "name, key enter, key F2, key ctrl+a, type the new name, key enter."),
}


def fill(x, values: dict):
    if isinstance(x, str):
        return re.sub(r"\{([A-Z]+)\}", lambda m: values.get(m.group(1), m.group(0)), x)
    if isinstance(x, list):
        return [fill(i, values) for i in x]
    if isinstance(x, dict):
        return {k: fill(v, values) for k, v in x.items()}
    return x


def close_new_windows(d: Desktop, before: set[int]) -> None:
    """Close what the demonstration opened, discarding changes (the result is already on disk)."""
    for _ in range(12):
        wins = [w for w in d.windows() if w["window_id"] not in before]
        if not wins:
            return
        w = sorted(wins, key=lambda w: " — " in (w.get("title") or "") or " - " in (w.get("title") or ""))[0]
        d.mcp.call("activate_window", window_id=w["window_id"])
        time.sleep(0.3)
        st = d.mcp.call("get_app_state", window_id=w["window_id"], include_screenshot=False, max_nodes=400, max_depth=30)
        names = {(e.get("name") or "").replace("’", "'"): e for e in st.get("accessibility_tree", [])
                 if e.get("role") in ("button", "push button")}
        b = next((names[n] for n in ("Don't Save", "Discard", "Close without saving") if n in names), None)
        if b:
            d.mcp.call("perform_action", element_index=b["index"])
        else:
            d.key("alt+f4" if "Nautilus" in (w.get("app_id") or "") else "ctrl+w", w)
        time.sleep(0.8)


def demonstrate(agent: HybridAgent, steps: list[dict]) -> str | None:
    els, shot = agent.observe(True)
    for s in steps:
        agent.mark()
        err = agent.step(s, els, shot)
        if err:
            return f"{step_label(s)}: {err}"
        els, shot = agent.observe(need_tree=s["do"] != "type")
    return None


def teach(name: str, agent: HybridAgent, d: Desktop) -> bool:
    sk = SKILLS[name]
    for f in DIR.glob("*"):
        if f.is_file():
            f.unlink()
    if sk.get("setup"):
        sk["setup"]()
    before = {w["window_id"] for w in d.windows()}
    agent.task_window, agent._prev_do = None, None
    t0 = time.perf_counter()
    err = demonstrate(agent, fill(sk["steps"], sk["values"]))
    secs = round(time.perf_counter() - t0, 1)
    if not err:
        time.sleep(0.8)
        out, verr = run_shell(fill(sk["verify"], sk["values"]))
        err = verr and f"verify failed: {verr[:200]}"
    close_new_windows(d, before)
    if err:
        print(f"  ✘ {name} ({secs} s): {err}", flush=True)
        return False
    memory.teach(sk["app"], sk["lesson"].replace("{secs}", str(secs)), source="claude demonstration")
    memory.add_workflow({"name": sk["name"], "when": sk["when"], "app": sk["app"], "variables": sk["variables"],
                         "steps": sk["steps"]})
    print(f"  ✔ {name}: verified in {secs} s, stored as a lesson and a workflow", flush=True)
    return True


def main(names: list[str]) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    with flat_pointer(), Takeover() as you:
        d = Desktop()
        own = next((w for w in d.windows() if w.get("focused")), None)
        agent = HybridAgent(d, apps=installed_apps(), on_step=lambda i: None, ignore_titles=("Glimpse and Jev",),
                            exclude_window_ids={own["window_id"]} if own else set())
        ok = 0
        try:
            for n in names:
                print(f"▶ {n}", flush=True)
                try:
                    ok += teach(n, agent, d)
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    print(f"  ✘ {n}: {type(e).__name__}: {e!s:.200}", flush=True)
        except KeyboardInterrupt:
            print(f"stopped: {you.reason or 'interrupted'}")
        print(f"\n{ok}/{len(names)} skills taught")
        d.mcp.close()


if __name__ == "__main__":
    main(sys.argv[1:] or list(SKILLS))
