"""Background processes for multistep tasks: a server, a long download or conversion, a watcher.

The planner starts one with a "spawn" step, reads its output and state with "proc", and ends it with "stop_proc"
once its work is done. Whatever is still running when the task ends is stopped, unless the planner asked to keep it
("keep": true, only when the task says to leave it running). Output goes to a log file per process.
"""

import os
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path


class Procs:
    def __init__(self, remote=None):
        """remote(cmd, timeout) -> (output, returncode): processes run there (the OSWorld VM) instead."""
        self.remote = remote
        self.items: dict[str, dict] = {}
        self.dir = Path(tempfile.mkdtemp(prefix="glimpse-procs-"))

    def _name(self, name: str) -> str:
        n = re.sub(r"[^\w.-]", "_", name or "")[:30] or f"p{len(self.items) + 1}"
        return n if n not in self.items or not self.alive(n) else f"{n}{len(self.items) + 1}"

    def spawn(self, cmd: str, name: str = "", keep: bool = False) -> str:
        name = self._name(name)
        if self.remote:
            log = f"/tmp/glimpse-{name}.log"
            out, rc = self.remote(f"cd ~ && nohup setsid bash -lc {sh_quote(cmd)} >{log} 2>&1 </dev/null & echo PID $!", 15)
            m = re.search(r"PID (\d+)", out or "")
            if rc != 0 or not m:
                raise RuntimeError(f"could not start: {(out or '').strip()[-200:]}")
            self.items[name] = {"cmd": cmd, "pid": int(m.group(1)), "log": log, "keep": keep, "t0": time.time()}
        else:
            log = self.dir / f"{name}.log"
            p = subprocess.Popen(["bash", "-lc", cmd], stdout=open(log, "w"), stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, cwd=str(Path.home()), start_new_session=True)
            self.items[name] = {"cmd": cmd, "proc": p, "pid": p.pid, "log": str(log), "keep": keep, "t0": time.time()}
        return name

    def alive(self, name: str) -> bool:
        it = self.items.get(name)
        if not it:
            return False
        if "proc" in it:
            return it["proc"].poll() is None
        out, _ = self.remote(f"kill -0 {it['pid']} 2>/dev/null && echo RUNNING", 10)
        return "RUNNING" in (out or "")

    def status(self, name: str, lines: int = 40) -> str:
        it = self.items.get(name)
        if not it:
            return f"no process named '{name}' (started: {', '.join(self.items) or 'none'})"
        if "proc" in it:
            rc = it["proc"].poll()
            try:
                tail = "\n".join(Path(it["log"]).read_text(errors="replace").splitlines()[-lines:])
            except OSError:
                tail = ""
        else:
            out, _ = self.remote(f"kill -0 {it['pid']} 2>/dev/null && echo RUNNING; tail -n {lines} {it['log']}", 10)
            rc = None if (out or "").startswith("RUNNING") else "ended"
            tail = (out or "").removeprefix("RUNNING").strip()
        state = "running" if rc is None else f"ended (exit {rc})"
        return f"[{name}] {state}, {time.time() - it['t0']:.0f} s, `{it['cmd'][:80]}`\n{tail[-3000:]}"

    def stop(self, name: str) -> str:
        it = self.items.get(name)
        if not it:
            return f"no process named '{name}'"
        if "proc" in it:
            if it["proc"].poll() is None:
                try:
                    os.killpg(it["pid"], signal.SIGTERM)  # the whole group: bash and what it started
                    it["proc"].wait(3)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(it["pid"], signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        else:
            self.remote(f"kill -TERM -- -{it['pid']} 2>/dev/null || kill {it['pid']} 2>/dev/null; true", 10)
        it["stopped"] = True
        return f"[{name}] stopped"

    def close(self) -> list[str]:
        """End of the task: stop everything not marked keep. Returns what was stopped."""
        done = []
        for name, it in self.items.items():
            if not it.get("keep") and not it.get("stopped") and self.alive(name):
                self.stop(name)
                done.append(name)
        return done


def sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"
