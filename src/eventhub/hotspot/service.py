"""Wifi hotspot via hostapd and dnsmasq."""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Any

from eventhub.core.config import AppConfig
from eventhub.core.templates import render_template
from eventhub.core.types import (
    ActionResult,
    HealthResult,
    IssueLevel,
    RenderedFile,
    ServiceStatus,
    ValidationIssue,
)
from eventhub.network.hardware import detect_hardware
from eventhub.network.roles import resolve_roles
from eventhub.service_api import ServiceModule


class HotspotService(ServiceModule):
    name = "hotspot"
    title = "Wifi Hotspot"
    description = "hostapd access point and dnsmasq DHCP/DNS."
    config_attr = "hotspot"
    systemd_units = ("eventhub-hostapd.service", "eventhub-dnsmasq.service")
    depends_on = ("network",)
    packages = ("hostapd", "dnsmasq")

    @property
    def hotspot(self) -> Any:
        return self.config.hotspot

    def interface(self, config: AppConfig | None = None) -> str:
        cfg = config or self.config
        if cfg.hotspot.interface not in {"auto", ""}:
            return cfg.hotspot.interface
        try:
            hardware = detect_hardware(self.runtime)
        except Exception:
            hardware = None
        return resolve_roles(cfg, hardware).ap

    def validate(self, config: AppConfig) -> list[ValidationIssue]:
        issues = super().validate(config)
        section = config.hotspot
        interface = self.interface(config)
        if section.enabled and not interface:
            issues.append(
                ValidationIssue(
                    IssueLevel.ERROR,
                    "no AP interface: set hotspot.interface or roles.ap",
                    "hotspot.interface",
                )
            )
        if interface:
            try:
                hardware = detect_hardware(self.runtime)
                names = {item.name for item in hardware.interfaces}
            except Exception:
                names = set()
            if names and interface not in names:
                issues.append(
                    ValidationIssue(
                        IssueLevel.WARNING,
                        f"AP interface {interface} is not present right now",
                        "hotspot.interface",
                    )
                )
        if section.enabled and config.airdrop.enabled:
            airdrop = config.airdrop.interface
            if airdrop not in {"auto", ""} and airdrop == interface:
                issues.append(
                    ValidationIssue(
                        IssueLevel.ERROR,
                        "the AirDrop interface must differ from the AP interface",
                        "airdrop.interface",
                    )
                )
        if config.network.enabled and config.network.uplink != "none":
            wan = resolve_roles(config).wan
            if wan and wan == interface:
                issues.append(
                    ValidationIssue(
                        IssueLevel.ERROR,
                        "the WAN uplink and the AP must use different interfaces",
                        "roles.wan",
                    )
                )
        return issues

    def _hostapd_context(self) -> dict[str, Any]:
        section = self.hotspot
        band = section.band
        if band == "auto":
            band = "5" if section.channel >= 32 else "2.4"
        hw_mode = section.hw_mode
        if hw_mode == "auto":
            hw_mode = "a" if band == "5" else "g"
        key_mgmt = {
            "wpa2": "WPA-PSK",
            "wpa3": "SAE",
            "wpa2-wpa3": "WPA-PSK SAE",
        }[section.auth_mode]
        return {
            "interface": self.interface(),
            "ssid": section.ssid,
            "country_code": section.country_code,
            "ieee80211d": section.ieee80211d,
            "hw_mode": hw_mode,
            "channel": section.channel,
            "hidden": section.hidden,
            "max_clients": section.max_clients,
            "client_isolation": section.client_isolation,
            "password": section.password.get_secret_value(),
            "key_mgmt": key_mgmt,
            "auth_mode": section.auth_mode,
            "band": band,
        }

    def _dnsmasq_context(self) -> dict[str, Any]:
        section = self.hotspot
        network = section.network
        netmask = str(network.netmask)
        return {
            "interface": self.interface(),
            "dhcp_start": section.dhcp_start,
            "dhcp_end": section.dhcp_end,
            "netmask": netmask,
            "lease_time": section.lease_time,
            "server_ip": section.server_ip,
            "domain": section.dns_domain,
            "dns_servers": self.config.network.dns_servers,
        }

    def render(self, config: AppConfig) -> list[RenderedFile]:
        interface = self.interface(config)
        if not interface:
            raise ValueError("no AP interface configured")
        section = config.hotspot
        section.interface = interface
        band = section.band
        if band == "auto":
            band = "5" if section.channel >= 32 else "2.4"
        hw_mode = section.hw_mode
        if hw_mode == "auto":
            hw_mode = "a" if band == "5" else "g"
        net = ipaddress.ip_network(section.subnet, strict=False)
        hostapd = render_template(
            "modules/hotspot/hostapd.conf.j2",
            interface=interface,
            ssid=section.ssid,
            country_code=section.country_code,
            ieee80211d=section.ieee80211d,
            hw_mode=hw_mode,
            channel=section.channel,
            hidden=section.hidden,
            max_clients=section.max_clients,
            client_isolation=section.client_isolation,
            password=section.password.get_secret_value(),
            key_mgmt={
                "wpa2": "WPA-PSK",
                "wpa3": "SAE",
                "wpa2-wpa3": "WPA-PSK SAE",
            }[section.auth_mode],
            auth_mode=section.auth_mode,
            band=band,
        )
        dnsmasq = render_template(
            "modules/hotspot/dnsmasq.conf.j2",
            interface=interface,
            dhcp_start=section.dhcp_start,
            dhcp_end=section.dhcp_end,
            netmask=str(net.netmask),
            lease_time=section.lease_time,
            server_ip=section.server_ip,
            domain=section.dns_domain,
            dns_servers=config.network.dns_servers,
        )
        return [
            RenderedFile(
                Path("/etc/hostapd/eventhub.conf"),
                hostapd,
                mode=0o600,
                sensitive=True,
            ),
            RenderedFile(
                Path("/etc/dnsmasq.d/eventhub.conf"),
                dnsmasq,
                mode=0o644,
            ),
        ]

    def apply(self) -> ActionResult:
        interface = self.interface()
        if not interface:
            return ActionResult.failure("no AP interface configured")
        if self.runtime.runner.which("hostapd") is None:
            return ActionResult.failure("hostapd is not installed")
        if self.runtime.runner.which("dnsmasq") is None:
            return ActionResult.failure("dnsmasq is not installed")
        if self.runtime.runner.which("rfkill") is not None:
            self.runtime.runner.run(["rfkill", "unblock", "wifi"], timeout=10)
        if self.runtime.runner.which("iw") is not None:
            reg = self.runtime.runner.run(
                ["iw", "reg", "set", self.hotspot.country_code], timeout=10
            )
            if not reg.ok:
                self.log.warning("could not set regulatory domain: %s", reg.stderr.strip())
        # The distro units may already own the radio or port 53.
        for unit in ("hostapd.service", "dnsmasq.service"):
            if self.runtime.systemd.is_active(unit):
                self.runtime.systemd.stop(unit)
        self.runtime.fs.mkdir("/etc/hostapd", mode=0o755)
        self.runtime.fs.mkdir("/etc/dnsmasq.d", mode=0o755)
        self.runtime.fs.mkdir("/var/lib/misc", mode=0o755)
        failures: list[str] = []
        for unit in self.systemd_units:
            if not self.runtime.systemd.restart(unit):
                failures.append(unit)
        if failures:
            return ActionResult.failure(
                "could not start " + ", ".join(failures),
                units=failures,
            )
        self.runtime.bus.emit(
            "hotspot.started",
            {"ssid": self.hotspot.ssid, "interface": interface},
            source="hotspot",
        )
        return ActionResult.success(f"hotspot {self.hotspot.ssid!r} running on {interface}")

    def status(self) -> ServiceStatus:
        status = super().status()
        if status.running:
            clients = self.clients()
            return ServiceStatus(
                name=status.name,
                installed=status.installed,
                enabled=status.enabled,
                running=status.running,
                healthy=status.healthy,
                detail=f"{len(clients)} client(s) on {self.interface()}",
                units=status.units,
                checked_at=status.checked_at,
            )
        return status

    def health_check(self) -> HealthResult:
        active = {
            unit: self.runtime.systemd.is_active(unit) for unit in self.systemd_units
        }
        if not all(active.values()):
            missing = [unit for unit, value in active.items() if not value]
            return HealthResult(False, "inactive: " + ", ".join(missing))
        interface = self.interface()
        if self.runtime.runner.which("hostapd_cli") is not None:
            result = self.runtime.runner.run(
                ["hostapd_cli", "-i", interface, "status"], timeout=8
            )
            if result.ok and "state=ENABLED" not in result.stdout:
                return HealthResult(False, f"hostapd state: {result.stdout.strip()[:200]}")
        return HealthResult(True, "access point enabled", {"interface": interface})

    def clients(self) -> list[dict[str, Any]]:
        interface = self.interface()
        if not interface or self.runtime.runner.which("iw") is None:
            return []
        result = self.runtime.runner.run(
            ["iw", "dev", interface, "station", "dump"], timeout=10
        )
        if not result.ok:
            return []
        clients: list[dict[str, Any]] = []
        current: dict[str, Any] = {}
        for raw in result.stdout.splitlines():
            line = raw.strip()
            if line.startswith("Station "):
                if current:
                    clients.append(current)
                current = {"mac": line.split()[1], "interface": interface}
            elif ":" in line and current:
                key, value = line.split(":", 1)
                key = key.strip()
                value = value.strip()
                if key in {"signal", "signal_avg"}:
                    current[key] = value
                elif key in {"rx bytes", "tx bytes", "connected time", "inactive time"}:
                    current[key.replace(" ", "_")] = value
        if current:
            clients.append(current)
        return clients

    def kick(self, mac: str) -> ActionResult:
        if not re.fullmatch(r"[0-9a-fA-F:]{17}", mac):
            return ActionResult.failure("invalid MAC address")
        interface = self.interface()
        if not interface:
            return ActionResult.failure("no AP interface")
        result = self.runtime.runner.run(
            ["iw", "dev", interface, "station", "del", mac], timeout=10
        )
        if not result.ok:
            return ActionResult.failure(result.stderr.strip() or "could not kick client")
        self.runtime.bus.emit("hotspot.client_kicked", {"mac": mac}, source="hotspot")
        return ActionResult.success(f"disconnected {mac}")


SERVICE = HotspotService
