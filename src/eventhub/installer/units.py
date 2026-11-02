"""Systemd unit rendering.

Only EventHub-owned units are rendered from templates.  Units shipped by
packages (hostapd, dnsmasq, smbd, avahi) are enabled but never overwritten.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from eventhub.core.config import AppConfig
from eventhub.core.templates import render_template
from eventhub.core.types import RenderedFile
from eventhub.installer.distro import Distro, PackageManager

# module -> template stems
UNIT_TEMPLATES: dict[str, tuple[str, ...]] = {
    "helper": ("eventhub-helper.service.j2",),    "dashboard": ("eventhub-dashboard.service.j2",),
    "fileserver_copyparty": ("eventhub-copyparty.service.j2",),
    "airdrop": ("eventhub-radio.service.j2", "eventhub-airdrop.service.j2"),
    "network": ("eventhub-network.service.j2",),
    "hotspot": ("eventhub-hostapd.service.j2", "eventhub-dnsmasq.service.j2"),
    "system": ("eventhub-metrics.service.j2",),
}

# module -> units owned by distro packages
EXTERNAL_UNITS: dict[str, tuple[str, ...]] = {
    "fileserver_smb": ("smbd.service",),
    "discovery": ("avahi-daemon.service",),
    "airdrop": ("owlyd.service",),
}


@dataclass(frozen=True, slots=True)
class UnitInfo:
    module: str
    name: str
    path: Path
    content: str
    external: bool = False


def _context(config: AppConfig, *, unit_path: Path) -> dict[str, object]:
    return {
        "config": config,
        "install": config.install,
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
    rendered: list[RenderedFile] = []
    for module, template_names in UNIT_TEMPLATES.items():
        if module not in enabled_modules:
            continue
        for template_name in template_names:
            unit_name = template_name.removesuffix(".j2")
            target = Path("/etc/systemd/system") / unit_name
            content = render_template(
                f"systemd/{template_name}",
                **_context(config, unit_path=target),
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
    for module, template_names in UNIT_TEMPLATES.items():
        if module in enabled:
            names.extend(name.removesuffix(".j2") for name in template_names)
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
    by_name: dict[str, str] = {}
    for module, template_names in UNIT_TEMPLATES.items():
        for template_name in template_names:
            by_name[template_name.removesuffix(".j2")] = module
    return [
        UnitInfo(
            module=by_name.get(item.path.name, ""),
            name=item.path.name,
            path=item.path,
            content=item.content,
        )
        for item in rendered
    ]
