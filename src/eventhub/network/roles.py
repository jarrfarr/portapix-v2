"""Role-to-interface resolution.

Roles are resolved in this order: explicit config, then hardware detection.
The result always contains concrete names or an empty string, never "auto".
"""

from __future__ import annotations

from dataclasses import dataclass

from eventhub.core.config import AppConfig, InterfaceRoles
from eventhub.core.context import Runtime
from eventhub.core.types import InterfaceInfo
from eventhub.network.hardware import HardwareReport, detect_hardware


@dataclass(frozen=True, slots=True)
class RoleMap:
    ap: str = ""
    airdrop: str = ""
    wan: str = ""
    lan: str = ""
    wan_kind: str = "none"
    interfaces: tuple[InterfaceInfo, ...] = ()

    def interface_for(self, role: str) -> str:
        return {
            "ap": self.ap,
            "airdrop": self.airdrop,
            "wan": self.wan,
            "lan": self.lan,
        }.get(role, "")

    def as_dict(self) -> dict[str, str]:
        return {
            "ap": self.ap,
            "airdrop": self.airdrop,
            "wan": self.wan,
            "lan": self.lan,
            "wan_kind": self.wan_kind,
        }


def _pick(
    configured: str,
    suggested: str | None,
    *,
    allow_auto: bool = True,
) -> str:
    if configured in {"auto", ""}:
        if suggested and suggested != "auto":
            return suggested
        return "" if allow_auto else "none"
    if configured == "none":
        return ""
    return configured


def resolve_roles(
    config: AppConfig, hardware: HardwareReport | None = None
) -> RoleMap:
    report = hardware
    if report is None:
        report = HardwareReport()
    roles: InterfaceRoles = config.roles
    suggested = report.suggested_roles
    by_name = {iface.name: iface for iface in report.interfaces}

    ap = _pick(roles.ap, suggested.get("ap"))
    if config.hotspot.interface not in {"auto", ""}:
        ap = config.hotspot.interface
    airdrop = _pick(roles.airdrop, suggested.get("airdrop"))
    if config.airdrop.interface not in {"auto", ""}:
        airdrop = config.airdrop.interface
    lan = _pick(roles.lan, suggested.get("lan"))
    wan = _pick(roles.wan, suggested.get("wan"))
    wifi_client = config.network.wifi_client_interface
    if wifi_client not in {"auto", ""}:
        wan = "" if wifi_client == "none" else wifi_client
    wan_kind = "none"
    if wan:
        info = by_name.get(wan)
        if info is not None:
            wan_kind = "wifi" if info.kind == "wifi" else "ethernet"
        else:
            wan_kind = "wifi" if wan == airdrop else "ethernet"
    return RoleMap(
        ap=ap,
        airdrop=airdrop,
        wan=wan,
        lan=lan,
        wan_kind=wan_kind,
        interfaces=report.interfaces,
    )


def resolve_roles_from_runtime(runtime: Runtime) -> RoleMap:
    try:
        hardware = detect_hardware(runtime)
    except Exception:
        hardware = HardwareReport()
    return resolve_roles(runtime.config, hardware)
