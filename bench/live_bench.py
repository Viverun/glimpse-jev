"""Live benchmark for jev: real desktop tasks with automatic checkers (OSWorld style).

Everything happens in ~/glimse-demo/bench or on a public test page. Each task runs through the real CLI
(`jev --quiet "<prompt>"`), then a checker scores the result 0/1 from the files on disk or the report.

  uv run python bench/live_bench.py            # all tasks
  uv run python bench/live_bench.py calc_total  # some tasks
"""

import csv
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

BENCH = Path.home() / "glimse-demo" / "bench"
LOG = Path.home() / ".cache" / "jev" / "last_run.json"
OUT = Path(__file__).parent / "results.jsonl"
ROWS = [("Notebook", 12, 3.5), ("Pen", 40, 1.25), ("Backpack", 3, 45.0), ("Stapler", 5, 12.0), ("Mug", 8, 9.5)]


def write_csv():
    with open(BENCH / "sales.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Product", "Quantity", "Price"])
        w.writerows(ROWS)


def ods_frame(path: Path):
    import pandas as pd
    return pd.read_excel(path, engine="odf")


def ods_xml(path: Path) -> str:
    import zipfile
    with zipfile.ZipFile(path) as z:
        return z.read("content.xml").decode()


def check_calc_total():
    df = ods_frame(BENCH / "sales_total.ods")
    got = {r["Product"]: float(r["Total"]) for _, r in df.iterrows()}
    return got == {p: q * pr for p, q, pr in ROWS}


def check_calc_bold():
    import re
    xml = ods_xml(BENCH / "styled.ods")
    bold = set(re.findall(r'<style:style style:name="([^"]+)"[^>]*>(?:(?!</style:style>).)*fo:font-weight="bold"', xml, re.S))
    row = re.search(r"<table:table-row[^>]*>(.*?)</table:table-row>", xml, re.S).group(1)
    names = re.findall(r'<table:table-cell[^>]*table:style-name="([^"]+)"', row)
    return len(names) >= 3 and all(n in bold for n in names[:3])


def check_writer_doc():
    import re
    import zipfile
    with zipfile.ZipFile(BENCH / "report.docx") as z:
        xml = z.read("word/document.xml").decode()
    paras = re.findall(r"<w:p[ >].*?</w:p>", xml, re.S)
    text = lambda p: "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", p))
    heading = next((p for p in paras if text(p).strip() == "Quarterly Report"), None)
    return heading is not None and re.search(r'w:pStyle w:val="(Heading1|Heading 1|Titre1|berschrift1)"', heading) is not None \
        and any(text(p).strip() == "Sales grew 12% this quarter." for p in paras)


def check_calc_sort():
    import pandas as pd
    df = pd.read_excel(BENCH / "sorted.xlsx")
    want = [p for p, q, pr in sorted(ROWS, key=lambda r: -r[2])]
    return list(df["Product"]) == want and list(df.columns[:3]) == ["Product", "Quantity", "Price"]


def setup_organize():
    for n in ("a.txt", "c.txt"):
        (BENCH / n).write_text("text\n")
    for n in ("b.csv", "d.csv"):
        (BENCH / n).write_text("x,y\n1,2\n")


def check_organize():
    return all((BENCH / "text" / n).exists() for n in ("a.txt", "c.txt")) and \
        all((BENCH / "data" / n).exists() for n in ("b.csv", "d.csv")) and \
        not any((BENCH / n).exists() for n in ("a.txt", "b.csv", "c.txt", "d.csv"))


TASKS = {
    "editor_write": dict(
        prompt=f"In Text Editor, write the line 'Benchmark line one.' and save it as {BENCH}/editor.txt",
        setup=lambda: None,
        check=lambda r: (BENCH / "editor.txt").read_text().strip() == "Benchmark line one."),
    "calc_total": dict(
        prompt=f"Open {BENCH}/sales.csv in LibreOffice Calc, add a column D with header Total equal to Quantity times "
               f"Price for every row, and save the sheet as {BENCH}/sales_total.ods",
        setup=write_csv, check=lambda r: check_calc_total()),
    "calc_bold": dict(
        prompt=f"Open {BENCH}/sales.csv in LibreOffice Calc, make the header row bold and save it in ODF format as {BENCH}/styled.ods",
        setup=write_csv, check=lambda r: check_calc_bold()),
    "files_folder": dict(
        prompt=f"In the Files app, create a folder named reports inside {BENCH} and move notes.txt from {BENCH} into it",
        setup=lambda: (BENCH / "notes.txt").write_text("meeting notes\n"),
        check=lambda r: (BENCH / "reports" / "notes.txt").exists() and not (BENCH / "notes.txt").exists()),
    "web_read": dict(
        prompt="In Zen Browser open https://example.com and tell me the main heading of the page",
        setup=lambda: None,
        check=lambda r: "example domain" in (r.get("report", "") + r.get("progress", "")).lower()),
    "multi_app": dict(
        prompt=f"Read the number in {BENCH}/value.txt, then in Text Editor write that number multiplied by 3 and save it as {BENCH}/tripled.txt",
        setup=lambda: (BENCH / "value.txt").write_text("14\n"),
        check=lambda r: (BENCH / "tripled.txt").read_text().strip() == "42"),
    # --- harder tasks ---
    "writer_doc": dict(
        prompt=f"In LibreOffice Writer, create a document whose first line is 'Quarterly Report' formatted as Heading 1, "
               f"followed by the paragraph 'Sales grew 12% this quarter.' and save it as {BENCH}/report.docx",
        setup=lambda: None, check=lambda r: check_writer_doc()),
    "calc_sort": dict(
        prompt=f"Open {BENCH}/sales.csv in LibreOffice Calc, sort the data rows by Price from highest to lowest "
               f"(keep the header row on top), and save it as {BENCH}/sorted.xlsx",
        setup=write_csv, check=lambda r: check_calc_sort()),
    "web_extract": dict(
        prompt="Using Zen Browser, find out on Wikipedia who designed the Python programming language and tell me",
        setup=lambda: None,
        check=lambda r: "guido van rossum" in (r.get("report", "") + r.get("progress", "")).lower()),
    "organize": dict(
        prompt=f"In the Files app, inside {BENCH}: create folders text and data, move the .txt files into text and the "
               f".csv files into data",
        setup=setup_organize, check=lambda r: check_organize()),
    "report_chain": dict(
        prompt=f"Compute the total revenue (sum of Quantity times Price) in {BENCH}/sales.csv, then in Text Editor write "
               f"the line 'Total revenue: X' with X the number, and save it as {BENCH}/revenue.txt",
        setup=write_csv,
        check=lambda r: (BENCH / "revenue.txt").read_text().strip() in ("Total revenue: 363", "Total revenue: 363.0", "Total revenue: 363.00")),
}


def run(name: str, env: dict | None = None, out: Path | None = None) -> dict:
    t = TASKS[name]
    shutil.rmtree(BENCH, ignore_errors=True)
    BENCH.mkdir(parents=True)
    t["setup"]()
    LOG.unlink(missing_ok=True)
    t0 = time.time()
    proc = subprocess.Popen(["glimpse", "--quiet", "--block", "Glimpse and Jev", "--max-minutes", "8", t["prompt"]],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(Path.home()),
                            env={**os.environ, "JEV_NO_LEARN": "1", **(env or {})}, start_new_session=True)
    try:
        _, err = proc.communicate(timeout=660)
    except subprocess.TimeoutExpired:
        import signal
        os.killpg(proc.pid, signal.SIGKILL)  # the whole group: no orphaned agent keeps using the desktop
        proc.communicate()
        err = "benchmark timeout (660 s)"
    proc.stderr_text = err
    secs = time.time() - t0
    log = json.loads(LOG.read_text()) if LOG.exists() else {}
    try:
        ok = bool(t["check"](log))
    except Exception as e:
        ok, log["check_error"] = False, repr(e)[:200]
    steps = log.get("steps", [])
    row = {"task": name, "ok": ok, "status": log.get("status"), "seconds": round(secs, 1),
           "actions": sum(1 for s in steps if s.get("label") and not str(s.get("label")).startswith(("◆", "read reply", "●"))),
           "plans": sum(1 for s in steps if str(s.get("label", "")).startswith("◆")),
           "timing": log.get("timing"), "failures": log.get("failures", [])[:4], "check_error": log.get("check_error"),
           "history": log.get("history", [])[-25:], "report": (log.get("report") or "")[:400],
           "time": time.strftime("%Y-%m-%d %H:%M")}
    with (out or OUT).open("a") as f:
        f.write(json.dumps(row) + "\n")
    if proc.returncode not in (0, None) and not log:
        row["stderr"] = (proc.stderr_text or "")[-400:]
    return row


BENCH_TITLES = ("sales.csv", "sales_total.ods", "styled.ods", "sorted.xlsx", "report.docx", "Untitled 2", "LibreOffice", "", "Save", "Save As", "Open",
                "Save Document?", "Keep current format", "Text Import", "Error", "Untitled 1", "Sort", "Sort Options")


def close_bench_windows(d) -> None:
    """Close non-LibreOffice windows the benchmark left: Text Editor on bench files, Files on bench folders,
    the bench's own Zen tabs. Matching is by title, so the user's own windows are never touched."""
    tries: dict[int, int] = {}
    for _ in range(25):
        wins = d.call("list_windows").get("windows", [])
        def bench_win(w):
            t, app = (w.get("title") or ""), (w.get("app_id") or "")
            if "TextEditor" in app:
                return "glimse-demo/bench" in t or t.startswith(("New Document", "Untitled", "Quarterly", "Total revenu",
                                                                  "Benchmark line", "12 (Draft)", "42 (Draft)"))
            if "Nautilus" in app:
                return t in ("bench", "reports", "text", "data", "glimse-demo")
            if app == "zen.desktop":
                return t.startswith(("Example Domain", "Python (programming language)", "Guido van Rossum"))
            return False
        w = next((w for w in wins if bench_win(w)), None)
        if not w:
            return
        d.call("activate_window", window_id=w["window_id"])
        time.sleep(0.3)
        # Files (GTK4) ignores ctrl+w; alt+f4 closes any window. Tabs (editor, browser) use ctrl+w first.
        key = "alt+f4" if "Nautilus" in (w.get("app_id") or "") or tries.get(w["window_id"], 0) >= 2 else "ctrl+w"
        tries[w["window_id"]] = tries.get(w["window_id"], 0) + 1
        d.call("press_key", key=key, window_id=w["window_id"])
        time.sleep(0.6)
        f = next((x for x in d.call("list_windows").get("windows", []) if x.get("focused")), None)
        if f and "TextEditor" in (f.get("app_id") or ""):
            st = d.call("get_app_state", window_id=f["window_id"], include_screenshot=False, max_nodes=300, max_depth=30)
            b = next((e for e in st.get("accessibility_tree", []) if e.get("role") == "button"
                      and (e.get("name") or "").lower() in ("discard", "close without saving")), None)
            if b:
                d.call("perform_action", element_index=b["index"])
                time.sleep(0.4)


def cleanup_windows():
    """Close benchmark documents properly (ctrl+q, Don't Save): a killed LibreOffice shows a recovery
    dialog on its next start. Leaves LibreOffice alone if it has non-benchmark windows."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "live"))
    from desktop_mcp import DesktopMCP
    d = DesktopMCP()
    try:
        close_bench_windows(d)
        for _ in range(8):
            lo = [w for w in d.call("list_windows").get("windows", [])
                  if "libreoffice" in (w.get("app_id") or "") or w.get("wm_class") in ("soffice", "libreoffice-calc")]
            if not lo:
                return
            titles = [(w.get("title") or "").split(" — ")[0] for w in lo]
            if not all(t in BENCH_TITLES or "Document Recovery" in t for t in titles):
                print(f"  (leaving LibreOffice open: non-benchmark windows {titles})")
                return
            btn = None
            # Dialogs first, each window read on its own: in a whole-process read the dialog's buttons can fall
            # past the node limit behind the sheet's thousands of cells.
            for w in sorted(lo, key=lambda w: " — " in (w.get("title") or "")):
                st = d.call("get_app_state", window_id=w["window_id"], include_screenshot=False, max_nodes=400, max_depth=30)
                # In priority order: a dialog's title-bar "Close" would cancel it, not discard the document.
                buttons = {(e.get("name") or "").replace("’", "'"): e for e in st.get("accessibility_tree", [])
                           if e.get("role") in ("button", "push button")}
                btn = next((buttons[n] for n in ("Don't Save", "Discard All", "Yes") if n in buttons), None) or \
                    (buttons.get("Close") if len(lo) > 1 and "Document Recovery" in (w.get("title") or "") else None)
                if btn:
                    break
            if btn:
                d.call("perform_action", element_index=btn["index"])
            else:
                d.call("activate_window", window_id=lo[0]["window_id"])
                time.sleep(0.3)
                d.call("press_key", key="ctrl+q", window_id=lo[0]["window_id"])
            time.sleep(1.2)
    finally:
        d.close()


if __name__ == "__main__":
    names = sys.argv[1:] or list(TASKS)
    results = []
    for n in names:
        print(f"▶ {n} …", flush=True)
        r = run(n)
        results.append(r)
        print(f"  {'✔' if r['ok'] else '✘'} {n}: {r['status']}  {r['seconds']} s  {r['actions']} actions  {r['plans']} plans"
              + (f"  check_error={r['check_error']}" if r.get("check_error") else ""), flush=True)
        if r.get("stderr"):
            print("  stderr:", r["stderr"])
        cleanup_windows()
        time.sleep(2)
    ok = sum(r["ok"] for r in results)
    print(f"\n{ok}/{len(results)} passed · mean {sum(r['seconds'] for r in results) / len(results):.1f} s")
