"""Data collected by the first-run setup wizard."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from eventhub.network.hardware import HardwareReport


@dataclass(slots=True)
class SetupState:
    hardware: HardwareReport | None = None
    roles: dict[str, str] = field(
        default_factory=lambda: {
            "ap": "auto",
            "airdrop": "auto",
            "wan": "auto",
            "lan": "auto",
        }
    )
    modules: dict[str, bool] = field(default_factory=dict)
    drive_uuid: str = "auto"
    ssid: str = "EventHub"
    password: str = ""
    country: str = "GB"
    hidden: bool = False
    admin_username: str = "admin"
    admin_password: str = ""

    def initialise(self, controller: Any, hardware: HardwareReport | None = None) -> None:
        config = controller.config
        self.hardware = hardware or self.hardware
        self.ssid = config.hotspot.ssid
        self.password = config.hotspot.password.get_secret_value()
        self.country = config.hotspot.country_code
        self.hidden = config.hotspot.hidden
        self.roles = {
            "ap": config.roles.ap,
            "airdrop": config.roles.airdrop,
            "wan": config.roles.wan,
            "lan": config.roles.lan,
        }
        for info in controller.infos():
            self.modules[info.name] = (
                config.module_enabled(info.name) if info.available else False
            )
        if self.hardware is not None and not self.hardware.airdrop_supported:
            self.modules["airdrop"] = False

    def build_patch(self, controller: Any) -> dict[str, Any]:
        patch: dict[str, Any] = {
            "roles": dict(self.roles),
            "hotspot": {
                "ssid": self.ssid,
                "password": self.password,
                "country_code": self.country,
                "hidden": self.hidden,
                "interface": self.roles["ap"],
            },
            "airdrop": {
                "enabled": bool(self.modules.get("airdrop", False)),
                "interface": self.roles["airdrop"],
            },
        }
        for info in controller.infos():
            if not info.available:
                continue
            section = patch.setdefault(info.config_section, {})
            section["enabled"] = bool(self.modules.get(info.name, False))
        if self.drive_uuid and self.drive_uuid != "auto":
            patch.setdefault("storage", {})["drive"] = {"uuid": self.drive_uuid}
        return patch
