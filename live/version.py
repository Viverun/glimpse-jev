"""glimpse's version: the one in pyproject.toml (bumped with each published update)."""

import tomllib
from pathlib import Path


def version() -> str:
    try:
        return tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text())["project"]["version"]
    except Exception:
        return "dev"
