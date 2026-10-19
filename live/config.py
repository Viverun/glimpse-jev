"""~/.config/jev/config.json: settings that outlive a session (stop key, defaults for flags)."""

import json
from pathlib import Path

PATH = Path.home() / ".config" / "jev" / "config.json"


def load() -> dict:
    try:
        return json.loads(PATH.read_text())
    except Exception:
        return {}


def get(key: str, default=None):
    return load().get(key, default)


def put(key: str, value) -> None:
    import os
    c = load()
    if value is None:
        c.pop(key, None)
    else:
        c[key] = value
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.touch(mode=0o600, exist_ok=True)
    os.chmod(PATH, 0o600)  # it can hold API keys
    PATH.write_text(json.dumps(c, indent=2))


# --- connection settings ---------------------------------------------------------------------------
# Each setting comes from the environment first (any of its names), else from this file. `glimpse setup` writes them.
SETTINGS = {
    "planner_base_url": ("JEV_PLANNER_BASE_URL", "OPENAI_BASE_URL", "AZURE_OPENAI_ENDPOINT"),
    "planner_api_key": ("JEV_PLANNER_API_KEY", "OPENAI_API_KEY", "AZURE_OPENAI_API_KEY"),
    "planner_model": ("JEV_PLANNER_MODEL", "AZURE_OPENAI_DEPLOYMENT"),
    "planner_effort": ("JEV_LUNA_EFFORT",),  # reasoning_effort; empty = don't send it
    "planner_tier": ("JEV_LUNA_TIER",),      # service_tier; empty = don't send it
    "typesafe_api_key": ("TYPESAFE_API_KEY",),
    "typesafe_base_url": ("TYPESAFE_BASE_URL",),
    "jev_model": ("JEV_MODEL",),
}
DEFAULTS = {"planner_model": "gpt-6-luna", "jev_model": "jev-preview"}
OLD_NAMES = {"planner_base_url": "luna_endpoint"}  # earlier key in this file


def setting(name: str) -> str:
    import os
    for env in SETTINGS[name]:
        if os.environ.get(env):
            return os.environ[env]
    c = load()
    return c.get(name) or c.get(OLD_NAMES.get(name, ""), "") or DEFAULTS.get(name, "")


def save_settings(values: dict) -> None:
    """Write settings (API keys included) to config.json, readable by you only."""
    import os
    c = load()
    c.update({k: v for k, v in values.items() if v is not None})
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.touch(mode=0o600, exist_ok=True)
    os.chmod(PATH, 0o600)
    PATH.write_text(json.dumps(c, indent=2))


def apply() -> None:
    """Export the saved settings to the environment names the rest of jev reads (env values win)."""
    import os
    for name, envs in SETTINGS.items():
        v = setting(name)
        if v and not any(os.environ.get(e) for e in envs):
            os.environ[envs[-1]] = v


def jev_client(**kw):
    """The TypeSafe (Jev) client, with the configured key, endpoint and model."""
    from typesafe_sdk import TypeSafeClient
    return TypeSafeClient(model=setting("jev_model"), api_key=setting("typesafe_api_key") or None,
                          base_url=setting("typesafe_base_url") or None, **kw)


def app_env() -> dict:
    """Environment for apps glimpse starts: GNOME_ACCESSIBILITY=1 makes Firefox-based browsers (Zen) and other
    apps publish their controls and page content to accessibility tools; without it Zen shows none."""
    import browsers
    return browsers.env()
