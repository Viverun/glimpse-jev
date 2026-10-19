"""Run the screenshot-free Jev + GPT-6-luna agent (computer-use-linux) on the live multi-app task.

Abort at any time with:  touch ~/glimse-demo/STOP
  uv run python live/run_a11y.py [--screenshots]
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from a11y_agent import Agent, Desktop  # noqa: E402
from livevm import flat_pointer  # noqa: E402
from run_live import INSTRUCTION, check, setup  # noqa: E402


def main() -> None:
    setup()  # fresh sales.csv, removes old outputs and STOP, opens it in LibreOffice Calc
    print(f"task: {INSTRUCTION}\n", flush=True)
    desktop = Desktop()
    agent = Agent(desktop, use_screenshots="--screenshots" in sys.argv)
    t0 = time.perf_counter()
    with flat_pointer():
        try:
            result = agent.run(INSTRUCTION)
        except KeyboardInterrupt as e:
            result = {"status": "stopped", "error": str(e), "timing": agent.timer.summary(), "luna_calls": None, "history": []}
        except Exception as e:
            result = {"status": "error", "error": repr(e)[:300], "timing": agent.timer.summary(), "luna_calls": None, "history": []}
    agent_s = time.perf_counter() - t0
    desktop.mcp.close()
    graded = check()
    out = Path(__file__).parent.parent / "results" / "live"
    out.mkdir(parents=True, exist_ok=True)
    (out / "last_a11y_run.json").write_text(json.dumps({"agent_s": round(agent_s, 1), **graded, **result}, indent=2))
    print(f"\nscore={graded['score']} checks={graded['checks']}\nstatus={result['status']} {result.get('error', '')}\n"
          f"agent_time={agent_s:.1f}s luna_calls={result.get('luna_calls')} timing={result['timing']}")


if __name__ == "__main__":
    main()
