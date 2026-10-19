"""Jev-only live task: open sales.csv in Calc, bold the header row, save as ODS.

Abort at any time with:  touch ~/glimse-demo/STOP
  uv run python live/run_jev.py
"""

import json
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from a11y_agent import Desktop  # noqa: E402
from jev_agent import JevAgent  # noqa: E402
from livevm import flat_pointer  # noqa: E402
from run_live import DEMO, ROWS  # noqa: E402

REPORT = DEMO / "sales_report.ods"
TASK = (
    "In LibreOffice Calc, sales.csv is open. Make the header row (Product, Quantity, Price) bold, "
    f"then save the sheet in ODF spreadsheet format as {REPORT}"
)


DEMO_TITLES = ("sales.csv", "sales_report.ods", "summary.txt", "LibreOffice", "", "Save", "Open", "Save Document?",
               "Keep current format", "Text Import", "Error")


def reset_libreoffice(desktop: Desktop) -> None:
    """Close LibreOffice from a previous run, but only if every LibreOffice window is one of ours."""
    lo = [w for w in desktop.windows() if "libreoffice" in (w.get("wm_class") or "") or w.get("wm_class") == "soffice"]
    if not lo:
        return
    titles = [(w.get("title") or "").split(" — ")[0] for w in lo]
    if all(t in DEMO_TITLES or "Document Recovery" in t for t in titles):
        subprocess.run(["pkill", "-f", "soffice.bin"], check=False)
        time.sleep(2)
    else:
        sys.exit(f"LibreOffice has other documents open ({titles}); close them first")


def setup(desktop: Desktop) -> dict:
    import csv
    reset_libreoffice(desktop)
    DEMO.mkdir(exist_ok=True)
    for name in ("sales_report.ods", "summary.txt", "STOP"):
        (DEMO / name).unlink(missing_ok=True)
    with open(DEMO / "sales.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Product", "Quantity", "Price"])
        w.writerows(ROWS)
    subprocess.Popen(["libreoffice", "--norestore", "--calc", str(DEMO / "sales.csv")], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    # Wait for the CSV import dialog, accept it, then wait for the sheet window: like an OSWorld task config.
    for _ in range(30):
        time.sleep(1)
        wins = [w for w in desktop.windows() if "libreoffice" in (w.get("wm_class") or "") or w.get("wm_class") == "soffice"]
        imp = next((w for w in wins if "Text Import" in (w.get("title") or "")), None)
        if imp:
            desktop.key("enter", imp)
            continue
        sheet = next((w for w in wins if (w.get("title") or "").startswith("sales.csv")), None)
        if sheet:
            return sheet
    sys.exit("LibreOffice did not open sales.csv")


def check() -> dict:
    checks = {"report_exists": REPORT.exists()}
    if REPORT.exists():
        with zipfile.ZipFile(REPORT) as z:
            checks["is_ods"] = z.read("mimetype").decode() == "application/vnd.oasis.opendocument.spreadsheet"
            xml = z.read("content.xml").decode()
        bold_styles = set(re.findall(r'<style:style style:name="([^"]+)"[^>]*>(?:(?!</style:style>).)*fo:font-weight="bold"', xml, re.S))
        first_row = re.search(r"<table:table-row[^>]*>(.*?)</table:table-row>", xml, re.S)
        cells = re.findall(r'<table:table-cell([^>]*)>', first_row.group(1)) if first_row else []
        styled = [re.search(r'table:style-name="([^"]+)"', c) for c in cells[:3]]
        row_style = re.search(r'<table:table-row[^>]*table:default-cell-style-name="([^"]+)"', xml)
        checks["header_bold"] = len(cells) >= 3 and all((m and m.group(1) in bold_styles) or (row_style and row_style.group(1) in bold_styles) for m in styled)
        checks["data_intact"] = all(p in xml for p, _, _ in ROWS)
    return {"score": float(all(checks.values())), "checks": checks}


def main() -> None:
    desktop = Desktop()
    sheet = setup(desktop)
    print(f"task: {TASK}\n", flush=True)
    out = Path(__file__).parent.parent / "results" / "live"
    agent = JevAgent(desktop, debug_dir=out / ("jev_" + time.strftime("%H%M%S")))
    agent.task_window = sheet
    t0 = time.perf_counter()
    with flat_pointer():
        try:
            result = agent.run(TASK)
        except KeyboardInterrupt as e:
            result = {"status": "stopped", "error": str(e), "steps": None, "history": [], "timing": {}}
    secs = time.perf_counter() - t0
    desktop.mcp.close()
    graded = check()
    (out / "last_jev_run.json").write_text(json.dumps({"agent_s": round(secs, 1), **graded, **result}, indent=2))
    print(f"\nscore={graded['score']} checks={graded['checks']}\nstatus={result['status']} steps={result['steps']} "
          f"agent_time={secs:.1f}s timing={result['timing']}")


if __name__ == "__main__":
    main()
