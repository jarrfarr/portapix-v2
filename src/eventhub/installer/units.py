"""Systemd unit rendering.

Only EventHub-owned units are rendered from templates.  Units shipped by
packages (hostapd, dnsmasq, smbd, avahi) are enabled but never overwritten.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from eventhub.core.config import AppConfig
from eventhub.core.types import RenderedFile
from eventhub.installer.distro import Distro, PackageManager

# module -> template stem
UNIT_TEMPLATES: dict[str, str] = {
    "helper": "eventhub-helper.service.j2",
    "dashboard": "eventhub-dashboard.service.j2",
    "fileserver_copyparty": "eventhub-copyparty.service.j2",
    "airdrop": "eventhub-airdrop.service.j2",
    "network": "eventhub-network.service.j2",
    "hotspot": "eventhub-hotspot.service.j2",
    "system": "eventhub-metrics.service.j2",
}

# module -> units owned by distro packages
EXTERNAL_UNITS: dict[str, tuple[str, ...]] = {
    "hotspot": ("hostapd.service", "dnsmasq.service"),
    "fileserver_smb": ("smbd.service",),
    "discovery": ("avahi-daemon.service",),
}


@dataclass(frozen=True, slots=True)
class UnitInfo:
    module: str
    name: str
    path: Path
    content: str
    external: bool = False


def templates_dir() -> Path | None:
    """Find templates in both editable checkouts and installed wheels."""

    package = Path(__file__).resolve()
    candidates = [
        package.parents[2] / "templates",  # src/eventhub/templates
        package.parents[3] / "templates",  # repo/templates
    ]
    for candidate in candidates:
        if (candidate / "systemd").is_dir():
            return candidate
    try:
        from importlib.resources import files

        resource = files("eventhub").joinpath("templates")
        path = Path(str(resource))
        if (path / "systemd").is_dir():
            return path
    except (ImportError, ModuleNotFoundError, TypeError):
        pass
    return None


def _environment() -> Environment:
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


def _context(config: AppConfig, *, unit_path: Path) -> dict[str, object]:
    return {
        "config": config,
        "config_path": config.install.config_path,
        "install_root": config.install.install_root,
        "venv": config.install.venv_path,
        "state_dir": config.state_dir,
        "data_root": config.data_root,
        "log_dir": config.log_dir,
        "events_root": config.events_root,
        "service_user": config.install.service_user,
        "service_group": config.install.service_group,
        "dashboard_user": config.install.dashboard_user,
        "airdrop_user": config.install.airdrop_user,
        "hotspot": config.hotspot,
        "dashboard": config.dashboard,
        "copyparty": config.fileserver_copyparty,
        "airdrop": config.airdrop,
        "network": config.network,
        "helper": config.helper,
        "unit_path": unit_path,
        "hostname": config.hostname,
    }


def render_units(
    config: AppConfig, modules: Iterable[str], distro: Distro | None = None
) -> list[RenderedFile]:
    enabled_modules = set(modules)
    env = _environment()
    rendered: list[RenderedFile] = []
    for module, template_name in UNIT_TEMPLATES.items():
        if module not in enabled_modules:
            continue
        unit_name = template_name.removesuffix(".j2")
        target = Path("/etc/systemd/system") / unit_name
        content = env.get_template(f"systemd/{template_name}").render(
            **_context(config, unit_path=target)
        )
        rendered.append(
            RenderedFile(
                path=target,
                content=content,
                mode=0o644,
                owner="root",
                group="root",
            )
        )
    return rendered


def unit_names(modules: Iterable[str], distro: Distro | None = None) -> list[str]:
    enabled = set(modules)
    names: list[str] = []
    for module, template_name in UNIT_TEMPLATES.items():
        if module in enabled:
            names.append(template_name.removesuffix(".j2"))
    for module, units in EXTERNAL_UNITS.items():
        if module not in enabled:
            continue
        for unit in units:
            if (
                unit == "smbd.service"
                and distro
                and distro.package_manager is PackageManager.PACMAN
            ):
                unit = "smb.service"
            names.append(unit)
    return list(dict.fromkeys(names))


def units_to_enable(
    config: AppConfig, modules: Iterable[str], distro: Distro | None = None
) -> list[str]:
    return unit_names(modules, distro)


def unit_status_info(config: AppConfig, modules: Iterable[str]) -> list[UnitInfo]:
    rendered = render_units(config, modules)
    return [
        UnitInfo(
            module=next(
                (m for m, t in UNIT_TEMPLATES.items() if t.removesuffix(".j2") == item.path.name),
                "",
            ),
            name=item.path.name,
            path=item.path,
            content=item.content,
        )
        for item in rendered
    ]
