"""Run the Jev + GPT-6-luna agent on the live GNOME desktop, on an OSWorld-style multi-app task.

The task only touches ~/glimse-demo. Abort at any time with:  touch ~/glimse-demo/STOP

  uv run python live/run_live.py
"""

import csv
import json
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).parent.parent / "agent_src"))

from agent.loop import Agent  # noqa: E402
from agent.models import Jev, Luna  # noqa: E402
from livevm import STOP_FILE, LiveVM, flat_pointer  # noqa: E402

DEMO = Path.home() / "glimse-demo"
ROWS = [("Notebook", 12, 3.50), ("Pen", 40, 1.25), ("Backpack", 3, 45.00), ("Stapler", 5, 12.00), ("Mug", 8, 9.50)]

INSTRUCTION = (
    f"The file {DEMO}/sales.csv has columns Product, Quantity, Price. Open it in LibreOffice Calc, "
    "add a column D with header 'Total' where each row is Quantity times Price, sort the data rows by Total "
    f"from highest to lowest, and save the sheet as {DEMO}/sales_report.ods (ODF spreadsheet). "
    "Then open the Text Editor app, write one line naming the product with the highest Total and that Total, "
    f"and save it as {DEMO}/summary.txt. "
    f"Do not use a terminal. Only work with files in {DEMO}. "
    "LibreOffice Calc has already been started with sales.csv. A terminal window showing this agent's own "
    "log may be visible: ignore it, never click or type in it, and never wait for it. "
    "To open other apps, press super and type the app name."
)


def setup() -> None:
    DEMO.mkdir(exist_ok=True)
    for name in ("sales_report.ods", "summary.txt", "STOP"):
        (DEMO / name).unlink(missing_ok=True)
    with open(DEMO / "sales.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Product", "Quantity", "Price"])
        w.writerows(ROWS)
    # Like OSWorld's task config: open the starting app before the agent takes over.
    subprocess.Popen(["libreoffice", "--calc", str(DEMO / "sales.csv")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    time.sleep(8)


def check() -> dict:
    """OSWorld-style scoring: 1.0 only if every requirement holds."""
    want = sorted(((p, q * pr) for p, q, pr in ROWS), key=lambda r: -r[1])
    checks = {}
    try:
        df = pd.read_excel(DEMO / "sales_report.ods", engine="odf")
        checks["report_exists"] = True
        checks["total_column"] = "Total" in df.columns
        got = list(zip(df["Product"], df["Total"].astype(float))) if checks["total_column"] else []
        checks["totals_correct_and_sorted"] = [(p, round(t, 2)) for p, t in got] == [(p, round(t, 2)) for p, t in want]
    except Exception as e:
        checks["report_exists"] = False
        checks["report_error"] = repr(e)[:200]
    summary = (DEMO / "summary.txt").read_text() if (DEMO / "summary.txt").exists() else ""
    top, top_total = want[0]
    checks["summary_names_top"] = top.lower() in summary.lower() and any(s in summary for s in ("135", "135.0", "135.00"))
    score = float(all(v for k, v in checks.items() if k != "report_error"))
    return {"score": score, "checks": checks}


def main() -> None:
    setup()
    print(f"task: {INSTRUCTION}\nabort: touch {STOP_FILE}\n", flush=True)
    out = Path(__file__).parent.parent / "results" / "live"
    agent = Agent(LiveVM(), Luna(grounded=True), Jev(), max_luna_calls=25, debug_dir=out / time.strftime("%Y%m%d-%H%M%S"))
    t0 = time.perf_counter()
    with flat_pointer():
        try:
            result = agent.run(INSTRUCTION)
        except Exception as e:
            result = {"status": "error", "error": repr(e), "timing": agent.timer.summary(), "luna_calls": None}
    agent_s = time.perf_counter() - t0
    graded = check()
    row = {"agent_s": round(agent_s, 1), **graded, **{k: v for k, v in result.items() if k != "history"}}
    (out / "last_run.json").write_text(json.dumps({**row, "history": result.get("history", [])}, indent=2))
    print(f"\nscore={graded['score']} checks={graded['checks']}\nagent_time={agent_s:.1f}s luna_calls={result.get('luna_calls')} timing={result['timing']}")


if __name__ == "__main__":
    main()
