"""`glimpse setup`: connect the planner (any OpenAI-compatible endpoint) and Jev (TypeSafe), test both, check the desktop.

Settings go to ~/.config/jev/config.json (mode 600). Environment variables still win over the file.
"""

import shutil
import subprocess

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
    def has(cmd):
        return bool(shutil.which(cmd))
    session = subprocess.run(["bash", "-c", "echo $XDG_SESSION_TYPE $XDG_CURRENT_DESKTOP"], capture_output=True, text=True).stdout
    return [
        ("GNOME on Wayland", "wayland" in session and "GNOME" in session, "jev drives GNOME Wayland desktops"),
        ("computer-use-linux", has("computer-use-linux"), "npm install -g @agent-sh/computer-use-linux"),
        ("ydotool", has("ydotool"), "sudo apt install ydotool, and run ydotoold"),
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
