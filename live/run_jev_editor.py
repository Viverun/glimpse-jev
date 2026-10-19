"""Jev-only live task: write a line in GNOME Text Editor and save it to a given path.

Runs its own Text Editor instance (--standalone), so the user's windows and session are untouched.
Abort at any time with:  touch ~/glimse-demo/STOP
  uv run python live/run_jev_editor.py
"""

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from a11y_agent import Desktop  # noqa: E402
from jev_agent import JevAgent  # noqa: E402
from livevm import flat_pointer  # noqa: E402

DEMO = Path.home() / "glimse-demo"
OUT_FILE = DEMO / "summary.txt"
LINE = "Backpack is the top seller with a total of 135."
TASK = f"In Text Editor, write the line '{LINE}' and save the file as {OUT_FILE}"


def setup(desktop: Desktop):
    DEMO.mkdir(exist_ok=True)
    for p in (OUT_FILE, DEMO / "STOP"):
        p.unlink(missing_ok=True)
    proc = subprocess.Popen(["gnome-text-editor", "--standalone"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    for _ in range(20):
        time.sleep(0.5)
        win = next((w for w in desktop.windows() if w.get("pid") == proc.pid), None)
        if win:
            time.sleep(0.5)
            desktop.key("ctrl+t", win)  # fresh empty tab, whatever draft the editor restored
            time.sleep(0.6)
            win = next((w for w in desktop.windows() if w.get("pid") == proc.pid), win)
            return proc, win
    proc.terminate()
    sys.exit("Text Editor did not open")


def check() -> dict:
    text = OUT_FILE.read_text() if OUT_FILE.exists() else None
    checks = {"file_exists": text is not None, "line_correct": text is not None and text.strip() == LINE}
    return {"score": float(all(checks.values())), "checks": checks, "content": text}


def main() -> None:
    desktop = Desktop()
    with flat_pointer():  # no screenshot flash or shutter sound, flat pointer; restored after
        proc, win = setup(desktop)
        print(f"task: {TASK}\n", flush=True)
        out = Path(__file__).parent.parent / "results" / "live"
        agent = JevAgent(desktop, debug_dir=out / ("jev_editor_" + time.strftime("%H%M%S")))
        agent.task_window = win
        t0 = time.perf_counter()
        try:
            result = agent.run(TASK)
        except KeyboardInterrupt as e:
            result = {"status": "stopped", "error": str(e), "steps": None, "history": [], "timing": {}}
        secs = time.perf_counter() - t0
    time.sleep(1.0)  # saving is asynchronous
    graded = check()
    proc.terminate()  # only our standalone instance
    desktop.mcp.close()
    (out / "last_jev_editor_run.json").write_text(json.dumps({"agent_s": round(secs, 1), **graded, **result}, indent=2))
    print(f"\nscore={graded['score']} checks={graded['checks']} content={graded['content']!r}\n"
          f"status={result['status']} steps={result['steps']} agent_time={secs:.1f}s timing={result['timing']}")


if __name__ == "__main__":
    main()
