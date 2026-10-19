"""Run glimpse on OSWorld tasks and score them with OSWorld's own evaluators.

Runs where the OSWorld VM runs (for example a GitHub codespace with KVM). Each task: OSWorld resets the VM and
sets the task up, glimpse does it through the OSWorld backend (live/osworld_backend.py), then OSWorld's checker
scores the result (0 to 1). Results go to bench/osworld_results.jsonl.

  python bench/osworld_run.py --osworld /tmp/osw/OSWorld --vm /tmp/osw/vm/Ubuntu.qcow2 \\
      --tasks test_small.json --domains os,libreoffice_calc --limit 5 --max-minutes 8

Needs JEV_PLANNER_BASE_URL, JEV_PLANNER_API_KEY, JEV_PLANNER_MODEL and TYPESAFE_API_KEY in the environment.
"""

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "live"))
OUT = Path(__file__).parent / "osworld_results.jsonl"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--osworld", required=True, help="the OSWorld checkout")
    ap.add_argument("--vm", required=True, help="the Ubuntu.qcow2 image")
    ap.add_argument("--tasks", default="test_small.json", help="a task list in evaluation_examples/")
    ap.add_argument("--domains", default="", help="comma-separated domains (default: all in the list)")
    ap.add_argument("--ids", default="", help="comma-separated task ids (overrides --domains)")
    ap.add_argument("--limit", type=int, default=0, help="at most this many tasks")
    ap.add_argument("--max-minutes", type=float, default=8)
    a = ap.parse_args()

    sys.path.insert(0, a.osworld)
    os.chdir(a.osworld)
    from desktop_env.desktop_env import DesktopEnv

    import osworld_backend
    from hybrid_agent import HybridAgent

    listing = json.loads((Path(a.osworld) / "evaluation_examples" / a.tasks).read_text())
    todo = [(d, i) for d, ids in listing.items() for i in ids
            if (not a.domains or d in a.domains.split(",")) and (not a.ids or i in a.ids.split(","))]
    if a.limit:
        todo = todo[: a.limit]
    print(f"{len(todo)} task(s)", flush=True)

    env = DesktopEnv(provider_name="docker", path_to_vm=a.vm, os_type="Ubuntu", action_space="pyautogui",
                     headless=True, require_a11y_tree=True)
    done = 0
    try:
        for domain, tid in todo:
            cfg = json.loads((Path(a.osworld) / "evaluation_examples" / "examples" / domain / f"{tid}.json").read_text())
            row = {"domain": domain, "id": tid, "instruction": cfg["instruction"], "time": time.strftime("%Y-%m-%d %H:%M")}
            t0 = time.perf_counter()
            try:
                env.reset(task_config=cfg)
                time.sleep(3)
                desktop = osworld_backend.make_desktop(f"http://{env.vm_ip}:{env.server_port}")
                steps = []
                agent = HybridAgent(desktop, apps={}, max_minutes=a.max_minutes, allow_terminals=True,
                                    blocked_words=(), on_step=steps.append)
                result = agent.run(cfg["instruction"])
                row.update(status=result["status"], plans=sum(1 for s in steps if s.get("kind") == "plan"),
                           actions=sum(1 for s in steps if s.get("kind") not in ("plan", "update")),
                           failures=result.get("failures", [])[:5], report=(result.get("report") or "")[:300],
                           timing=result.get("timing"))
                time.sleep(2)
                row["score"] = float(env.evaluate())
            except Exception as e:
                row.update(status="error", score=0.0, error=f"{type(e).__name__}: {e!s:.300}", trace=traceback.format_exc()[-1500:])
            row["seconds"] = round(time.perf_counter() - t0, 1)
            done += row["score"] > 0
            with OUT.open("a") as f:
                f.write(json.dumps(row) + "\n")
            print(f"{'✔' if row['score'] > 0 else '✘'} {domain}/{tid[:8]}  score {row['score']:.2f}  {row['seconds']} s  "
                  f"{row.get('status')}  {row.get('error', '')[:120]}", flush=True)
    finally:
        env.close()
    print(f"\n{done}/{len(todo)} scored above 0", flush=True)


if __name__ == "__main__":
    main()
