"""`glimpse setup`: connect the planner (any OpenAI-compatible endpoint) and Jev (TypeSafe), test both, check the desktop.

Settings go to ~/.config/jev/config.json (mode 600). Environment variables still win over the file.
"""

import os
import shutil
import subprocess
from pathlib import Path

import config

QUESTIONS = [
    ("planner_base_url", "Planner base URL (OpenAI-compatible, e.g. https://api.openai.com/v1)", False),
    ("planner_api_key", "Planner API key", True),
    ("planner_model", "Planner model (it must read images and return JSON)", False),
    ("planner_effort", "Reasoning effort: low, medium, high, or empty for none", False),
    ("typesafe_api_key", "TypeSafe (Jev) API key", True),
    ("typesafe_base_url", "TypeSafe base URL (empty for the default)", False),
]


def _mask(v: str) -> str:
    return (v[:4] + "…" + v[-4:]) if len(v) > 12 else ("set" if v else "")


def test_planner(values: dict) -> str | None:
    from openai import OpenAI
    try:
        r = OpenAI(base_url=values["planner_base_url"], api_key=values["planner_api_key"], timeout=40).chat.completions.create(
            model=values["planner_model"], response_format={"type": "json_object"},
            **({"reasoning_effort": values["planner_effort"]} if values.get("planner_effort") else {}),
            messages=[{"role": "user", "content": 'Reply with JSON only: {"ok": true}'}])
        return None if "ok" in (r.choices[0].message.content or "") else "unexpected reply"
    except Exception as e:
        return f"{type(e).__name__}: {e!s:.160}"


def test_jev(values: dict) -> str | None:
    from typesafe_sdk import Noul, TypeSafeClient
    try:
        c = TypeSafeClient(model=values.get("jev_model") or "jev-preview", api_key=values["typesafe_api_key"],
                           base_url=values.get("typesafe_base_url") or None, timeout=30)
        c.system_one(state={"x": "the sky is blue"}, questions={"q": Noul(instructions="Is `x` about the sky?")})
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e!s:.160}"


def desktop_checks() -> list[tuple[str, bool, str]]:
    """Everything that can block glimpse, with the fix for each. (name, ok, how to fix)"""
    import os
    import re as _re

    def has(cmd):
        return bool(shutil.which(cmd))

    def out(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            return ""
    session = f"{os.environ.get('XDG_SESSION_TYPE', '')} {os.environ.get('XDG_CURRENT_DESKTOP', '')}"
    cul = out(["npm", "ls", "-g", "--depth=0", "@agent-sh/computer-use-linux"]) if has("npm") else ""
    ver = _re.search(r"computer-use-linux@(\d+)\.(\d+)\.(\d+)", cul)
    ver_ok = bool(ver) and tuple(map(int, ver.groups())) >= (0, 7, 18)
    import browsers
    unready = [name for name, pid, fam in browsers.running() if not browsers.process_ready(pid, fam)]
    in_input = "input" in out(["id", "-nG"]).split()
    sock = Path(f"/run/user/{os.getuid()}/.ydotool_socket").exists()
    return [
        ("GNOME on Wayland", "wayland" in session and "GNOME" in session, "glimpse operates GNOME on Wayland sessions"),
        ("computer-use-linux 0.7.18 or newer", ver_ok or (not ver and has("computer-use-linux")),
         "npm install -g @agent-sh/computer-use-linux@latest"),
        ("ydotool daemon running", sock, "systemctl --user enable --now ydotoold"),
        ("you are in the 'input' group (stop key, pause)", in_input, "sudo usermod -aG input $USER, then log out and in"),
        ("accessibility on", out(["gsettings", "get", "org.gnome.desktop.interface", "toolkit-accessibility"]) == "true",
         "gsettings set org.gnome.desktop.interface toolkit-accessibility true"),
        ("apps publish their controls (GNOME_ACCESSIBILITY / ACCESSIBILITY_ENABLED)",
         Path.home().joinpath(".config/environment.d/90-glimpse.conf").exists() or os.environ.get("GNOME_ACCESSIBILITY") == "1",
         "echo GNOME_ACCESSIBILITY=1 > ~/.config/environment.d/90-glimpse.conf, then log out and in"),
        ("open browsers publish page content", not unready,
         f"log out and in, or close {', '.join(unready)} and start it once with: GNOME_ACCESSIBILITY=1 "
         f"ACCESSIBILITY_ENABLED=1 {' '.join(unready)} &   (reopening from the dock is not enough before the next login)"),
        ("gtk-launch", has("gtk-launch"), "sudo apt install libgtk-3-bin"),
        ("LibreOffice (optional)", has("libreoffice"), "needed for spreadsheet and document tasks"),
    ]


def run(console) -> None:
    current = {k: config.setting(k) for k in config.SETTINGS}
    console.print("[bold]glimpse setup[/]  Press Enter to keep the value in brackets.\n")
    values = {}
    for key, label, secret in QUESTIONS:
        shown = _mask(current[key]) if secret else current[key]
        ans = console.input(f"{label} [dim][{shown}][/]: ", password=secret).strip()
        values[key] = ans or current[key]
    values["jev_model"] = current["jev_model"]
    console.print()
    with console.status("testing the planner…"):
        p_err = test_planner(values) if values["planner_base_url"] and values["planner_api_key"] else "not set"
    console.print(("[green]✔ planner works[/]" if not p_err else f"[red]✘ planner: {p_err}[/]  [dim](--jev-only works without it)[/]"))
    with console.status("testing Jev…"):
        j_err = test_jev(values) if values["typesafe_api_key"] else "not set"
    console.print("[green]✔ Jev works[/]" if not j_err else f"[red]✘ Jev: {j_err}[/]")
    config.save_settings({k: v for k, v in values.items() if v != config.DEFAULTS.get(k)})
    console.print(f"[dim]saved to {config.PATH} (only you can read it)[/]\n")
    grant_remote_interaction(console)
    restart_unready_browsers(console)
    console.print()
    for name, ok, fix in desktop_checks():
        console.print(f"{'[green]✔' if ok else '[yellow]![/yellow][yellow]'} {name}[/]" + ("" if ok else f"  [dim]{fix}[/]"))
    if not (p_err or j_err):
        console.print('\nReady. Try: [bold]glimpse "open example.com in the browser and tell me the heading"[/]')


GRANT_DIR = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "computer-use-linux"


def remote_grant_saved() -> bool:
    return any(GRANT_DIR.glob("remote-desktop-*.token"))


def grant_remote_interaction(console) -> None:
    """GNOME must ask once before an app may send keys ("Allow Remote Interaction"); no app can skip that.
    Ask it now, during setup, so the question never interrupts a task. glimpse then remembers the grant."""
    if remote_grant_saved():
        console.print("[green]✔ remote interaction already allowed[/]")
        return
    console.print("\n[bold]GNOME asks one time[/] before glimpse may send keys. In the dialog that opens now:\n"
                  "  1. switch on [bold]Allow Remote Interaction[/]   2. keep [bold]Remember This Selection[/] on   3. push [bold]Share[/]")
    if console.input("Open the dialog now? [dim][Y/n][/] ").strip().lower().startswith("n"):
        return
    try:
        from desktop_mcp import DesktopMCP
        m = DesktopMCP()
        try:
            w = next((w for w in m.call("list_windows").get("windows", []) if w.get("focused")), None)
            m.call("press_key", key="shift", **({"window_id": w["window_id"]} if w else {}))  # harmless: opens the dialog
        finally:
            m.close()
    except Exception as e:
        console.print(f"[yellow]! could not open the dialog: {e!s:.120}[/]")
    console.print("[green]✔ remote interaction allowed and remembered[/]" if remote_grant_saved()
                  else "[yellow]! not saved yet: glimpse asks again on its first task[/]")


def restart_unready_browsers(console) -> None:
    """A browser that started before the setup shows glimpse no page content until it starts again with the
    accessibility settings. Offer to restart it now (a normal close: it reopens its tabs), so no logout is needed."""
    import signal
    import time

    import browsers
    unready = [(n, pid, fam) for n, pid, fam in browsers.running() if not browsers.process_ready(pid, fam)]
    if not unready:
        return
    names = ", ".join(n for n, _, _ in unready)
    if console.input(f"\n{names} started before the setup, so glimpse cannot read its pages. Restart it now? "
                     "Your tabs come back. [dim][Y/n][/] ").strip().lower().startswith("n"):
        return
    for name, pid, fam in unready:
        try:
            exe = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[0].decode()  # the launcher, as you started it
            if "/snap/" in exe:  # a Snap browser starts through /snap/bin, not its internal path
                exe = f"/snap/bin/{Path(exe).name}"
            os.kill(pid, signal.SIGTERM)  # a normal close: the browser saves its session
            for _ in range(50):
                if not Path(f"/proc/{pid}").exists():
                    break
                time.sleep(0.2)
            cmd = [exe] + ([browsers.CHROMIUM_FLAG] if fam == "chromium" else [])
            subprocess.Popen(cmd, env=browsers.env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            console.print(f"[green]✔ restarted {name}: glimpse can read its pages[/]")
        except Exception as e:
            console.print(f"[yellow]! could not restart {name}: {e!s:.100}. Close it and open it again.[/]")
