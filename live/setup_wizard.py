"""`glimpse setup`: connect the planner (any OpenAI-compatible endpoint) and Jev (TypeSafe), test both, check the desktop.

Settings go to ~/.config/jev/config.json (mode 600). Environment variables still win over the file.
"""

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
         f"restart {', '.join(unready)} once (started before glimpse set accessibility up)"),
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
    for name, ok, fix in desktop_checks():
        console.print(f"{'[green]✔' if ok else '[yellow]![/yellow][yellow]'} {name}[/]" + ("" if ok else f"  [dim]{fix}[/]"))
    if not (p_err or j_err):
        console.print('\nReady. Try: [bold]glimpse "open example.com in the browser and tell me the heading"[/]')
