"""Access to the project's Jinja2 templates in checkouts and installed wheels."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined


@lru_cache(maxsize=1)
def templates_dir() -> Path | None:
    package = Path(__file__).resolve()
    candidates = [
        package.parents[1] / "templates",  # src/eventhub/templates (installed wheel)
        package.parents[3] / "templates",  # repo/templates (editable checkout)
    ]
    for candidate in candidates:
        if (candidate / "modules").is_dir() or (candidate / "systemd").is_dir():
            return candidate
    try:
        from importlib.resources import files

        resource = files("eventhub").joinpath("templates")
        path = Path(str(resource))
        if path.is_dir():
            return path
    except (ImportError, ModuleNotFoundError, TypeError):
        pass
    return None


@lru_cache(maxsize=1)
def environment() -> Environment:
    root = templates_dir()
    if root is None:
        raise FileNotFoundError("EventHub templates directory not found")
    return Environment(
        loader=FileSystemLoader(str(root)),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_template(name: str, **context: Any) -> str:
    return environment().get_template(name).render(**context)
