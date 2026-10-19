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


class OSWorldBench:
    """OSWorld as a benchmark for glimpse (and for bench/harness2.py): tasks by name, run(name, env) -> row.
    One VM is kept for all runs; OSWorld resets it before each task."""

    PASSWORD = "This is the OSWorld test VM: the user's password (for sudo) is 'password'; use `echo password | sudo -S ...` in shell steps."

    def __init__(self, osworld: str, vm: str, tasks: str = "test_small.json", domains: str = "", ids: str = "",
                 limit: int = 0, max_minutes: float = 8):
        sys.path.insert(0, osworld)
        os.chdir(osworld)
        self.root, self.vm, self.max_minutes, self.env = Path(osworld), vm, max_minutes, None
        listing = json.loads((self.root / "evaluation_examples" / tasks).read_text())
        todo = [(d, i) for d, ids_ in listing.items() for i in ids_
                if (not domains or d in domains.split(",")) and (not ids or i in ids.split(","))]
        self.todo = todo[:limit] if limit else todo
        # short names for harness2 ("os/5ea617a3"), each with its OSWorld config
        self.TASKS = {}
        for d, i in self.todo:
            cfg = json.loads((self.root / "evaluation_examples" / "examples" / d / f"{i}.json").read_text())
            self.TASKS[f"{d}/{i[:8]}"] = {"prompt": cfg["instruction"], "domain": d, "cfg": cfg}

    def domain(self, name: str) -> str:
        return self.TASKS[name]["domain"]

    def _env(self):
        if self.env is None:
            from desktop_env.desktop_env import DesktopEnv
            self.env = DesktopEnv(provider_name="docker", path_to_vm=self.vm, os_type="Ubuntu", action_space="pyautogui",
                                  headless=True, require_a11y_tree=True)
        return self.env

    def run(self, name: str, env: dict | None = None, out: Path | None = None) -> dict:
        """One task: reset the VM, run glimpse with `env` set (e.g. JEV_HARNESS_OVERLAY), score with OSWorld."""
        import osworld_backend
        from hybrid_agent import HybridAgent
        t, vm = self.TASKS[name], self._env()
        cfg = t["cfg"]
        row = {"task": name, "domain": t["domain"], "id": cfg["id"], "instruction": cfg["instruction"],
               "time": time.strftime("%Y-%m-%d %H:%M"), "ok": False, "plans": 0, "actions": 0}
        saved = {k: os.environ.get(k) for k in (env or {})}
        os.environ.update(env or {})
        t0 = time.perf_counter()
        try:
            vm.reset(task_config=cfg)
            time.sleep(3)
            desktop = osworld_backend.make_desktop(f"http://{vm.vm_ip}:{vm.server_port}")
            steps = []
            agent = HybridAgent(desktop, apps=desktop.server.apps(), max_minutes=self.max_minutes, allow_terminals=True,
                                blocked_words=(), on_step=steps.append)
            agent.extra.append(self.PASSWORD)
            result = agent.run(cfg["instruction"])
            row.update(status=result["status"], plans=sum(1 for s in steps if s.get("kind") == "plan"),
                       actions=sum(1 for s in steps if s.get("kind") not in ("plan", "update")),
                       failures=result.get("failures", [])[:5], report=(result.get("report") or "")[:300],
                       history=(result.get("history") or [])[-40:], timing=result.get("timing"))
            time.sleep(2)
            row["score"] = float(vm.evaluate())
        except Exception as e:
            row.update(status="error", score=0.0, error=f"{type(e).__name__}: {e!s:.300}", trace=traceback.format_exc()[-1500:])
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        row["ok"] = row["score"] > 0
        row["seconds"] = round(time.perf_counter() - t0, 1)
        with (out or OUT).open("a") as f:
            f.write(json.dumps(row) + "\n")
        return row

    def cleanup_windows(self) -> None:
        pass  # OSWorld resets the VM before every task

    def close(self) -> None:
        if self.env is not None:
            self.env.close()


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
    bench = OSWorldBench(a.osworld, a.vm, a.tasks, a.domains, a.ids, a.limit, a.max_minutes)
    print(f"{len(bench.TASKS)} task(s)", flush=True)
    done = 0
    try:
        for name in bench.TASKS:
            row = bench.run(name)
            done += row["ok"]
            print(f"{'✔' if row['ok'] else '✘'} {name}  score {row['score']:.2f}  {row['seconds']} s  "
                  f"{row.get('status')}  {row.get('error', '')[:120]}", flush=True)
    finally:
        bench.close()
    print(f"\n{done}/{len(bench.TASKS)} scored above 0", flush=True)


if __name__ == "__main__":
    main()
