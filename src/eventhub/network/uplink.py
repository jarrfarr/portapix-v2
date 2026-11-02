"""Uplink selection, connectivity probing, wifi scanning and connection."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from eventhub.core.config import AppConfig, WifiCredential
from eventhub.core.context import Runtime
from eventhub.core.templates import render_template
from eventhub.core.types import ActionResult, InterfaceInfo
from eventhub.network.hardware import HardwareReport, detect_hardware
from eventhub.network.roles import RoleMap, resolve_roles
from eventhub.network.state import UplinkState

_SYSFS = Path("/sys/class/net")


@dataclass(frozen=True, slots=True)
class WifiNetwork:
    ssid: str
    bssid: str = ""
    signal_dbm: int = 0
    security: str = "open"
    channel: int = 0
    hidden: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "ssid": self.ssid,
            "bssid": self.bssid,
            "signal_dbm": self.signal_dbm,
            "security": self.security,
            "channel": self.channel,
            "hidden": self.hidden,
        }


class UplinkManager:
    def __init__(self, runtime: Runtime, config: AppConfig | None = None) -> None:
        self.runtime = runtime
        self.config = config or runtime.config
        self.log = runtime.logger.getChild("network.uplink")

    # -- interfaces ----------------------------------------------------
    def hardware(self) -> HardwareReport:
        try:
            return detect_hardware(self.runtime)
        except Exception:
            return HardwareReport()

    def roles(self, hardware: HardwareReport | None = None) -> RoleMap:
        return resolve_roles(self.config, hardware or self.hardware())

    def _interface_info(
        self, name: str, hardware: HardwareReport | None = None
    ) -> InterfaceInfo | None:
        report = hardware or self.hardware()
        return next((item for item in report.interfaces if item.name == name), None)

    def interface_state(self, name: str) -> str:
        if not name:
            return "missing"
        try:
            return self.runtime.fs.read_text(_SYSFS / name / "operstate").strip()
        except (OSError, FileNotFoundError):
            return "missing"

    def interface_carrier(self, name: str) -> bool:
        state = self.interface_state(name)
        return state in {"up", "unknown"}

    # -- connectivity --------------------------------------------------
    def probe(self, urls: list[str] | None = None) -> bool | None:
        """Return True/False for internet, or None when no probe tool exists."""

        if self.runtime.runner.which("curl") is None:
            return None
        targets = urls or self.config.network.connectivity_urls
        timeout = str(int(self.config.network.connectivity_timeout))
        for url in targets:
            result = self.runtime.runner.run(
                [
                    "curl",
                    "-fsS",
                    "--max-time",
                    timeout,
                    "-o",
                    "/dev/null",
                    url,
                ],
                timeout=self.config.network.connectivity_timeout + 2,
            )
            if result.ok:
                return True
        return False

    def wifi_connected(self, name: str) -> bool:
        if not name:
            return False
        if self.runtime.runner.which("iw") is not None:
            result = self.runtime.runner.run(["iw", "dev", name, "link"], timeout=8)
            if result.ok and "Connected" in result.stdout:
                return True
        if self.runtime.runner.which("wpa_cli") is not None:
            result = self.runtime.runner.run(["wpa_cli", "-i", name, "status"], timeout=8)
            if result.ok and "wpa_state=COMPLETED" in result.stdout:
                return True
        return False

    def select(self) -> UplinkState:
        mode = self.config.network.uplink
        if mode == "none":
            return UplinkState(kind="offline", detail="uplink disabled in config")
        hardware = self.hardware()
        roles = self.roles(hardware)
        candidates: list[tuple[str, str]] = []
        wan_info = self._interface_info(roles.wan, hardware)
        if mode in {"auto", "wan"} and roles.wan:
            if wan_info is not None and wan_info.kind == "wifi":
                candidates.append(("wifi", roles.wan))
            else:
                candidates.append(("wan", roles.wan))
        wifi_iface = self.config.network.wifi_client_interface
        if wifi_iface in {"auto", ""}:
            wifi_iface = roles.wan if wan_info and wan_info.kind == "wifi" else ""
        if mode in {"auto", "wifi"} and wifi_iface and wifi_iface != roles.wan:
            candidates.append(("wifi", wifi_iface))
        if not candidates:
            return UplinkState(kind="offline", detail="no uplink interface")
        last_detail = "no candidate online"
        for kind, name in candidates:
            state = self.interface_state(name)
            if state == "missing":
                last_detail = f"{name} is missing"
                continue
            if kind == "wifi" and not self.wifi_connected(name):
                last_detail = f"{name} is not associated"
                continue
            if not self.interface_carrier(name):
                last_detail = f"{name} has no carrier"
                continue
            online = self.probe()
            if online:
                return UplinkState(
                    kind=kind,
                    interface=name,
                    online=True,
                    detail=f"{kind} {name} online",
                )
            if online is None:
                return UplinkState(
                    kind=kind,
                    interface=name,
                    online=False,
                    detail=f"{name} is up but no connectivity probe tool is available",
                )
            last_detail = f"{name} is up but internet is unreachable"
        return UplinkState(kind="offline", detail=last_detail)

    # -- wifi scan / connect ------------------------------------------
    def scan(self, interface: str = "") -> list[WifiNetwork]:
        name = interface or self.roles().wan
        if not name:
            return []
        if self.runtime.runner.which("iw") is None:
            return []
        result = self.runtime.runner.run(
            ["iw", "dev", name, "scan"], timeout=30
        )
        if not result.ok:
            return []
        networks: dict[str, WifiNetwork] = {}
        current: dict[str, object] = {}
        for raw_line in result.stdout.splitlines():
            line = raw_line.strip()
            if line.startswith("BSS "):
                self._commit(networks, current)
                current = {"bssid": line.split()[1].split("(")[0]}
            elif line.startswith("signal:"):
                match = re.search(r"(-?\d+)", line)
                if match:
                    current["signal"] = int(match.group(1))
            elif line.startswith("SSID:"):
                current["ssid"] = line.split(":", 1)[1].strip()
            elif line.startswith("DS Parameter set: channel"):
                match = re.search(r"(\d+)", line)
                if match:
                    current["channel"] = int(match.group(1))
            elif line.startswith(("RSN:", "WPA:")):
                current["security"] = "wpa2/wpa3"
            elif line.startswith("capability:") and "Privacy" in line:
                current["security"] = "wep"
            elif line.startswith("* SSID length:") and not current.get("ssid"):
                current["hidden"] = True
        self._commit(networks, current)
        return sorted(networks.values(), key=lambda item: item.signal_dbm, reverse=True)

    @staticmethod
    def _commit(networks: dict[str, WifiNetwork], raw: dict[str, object]) -> None:
        ssid = str(raw.get("ssid", "")).strip()
        if not ssid:
            return
        network = WifiNetwork(
            ssid=ssid,
            bssid=str(raw.get("bssid", "")),
            signal_dbm=int(raw.get("signal", 0)),  # type: ignore[arg-type]
            security=str(raw.get("security", "open")),
            channel=int(raw.get("channel", 0)),  # type: ignore[arg-type]
            hidden=bool(raw.get("hidden", False)),
        )
        existing = networks.get(ssid)
        if existing is None or network.signal_dbm > existing.signal_dbm:
            networks[ssid] = network

    def wifi_interface(self) -> str:
        roles = self.roles()
        explicit = self.config.network.wifi_client_interface
        if explicit not in {"auto", ""}:
            return "" if explicit == "none" else explicit
        info = self._interface_info(roles.wan)
        if info is not None and info.kind == "wifi":
            return roles.wan
        return ""

    def connect(self, ssid: str, password: str) -> ActionResult:
        interface = self.wifi_interface()
        if not interface:
            return ActionResult.failure(
                "no wifi client interface is assigned; set network.wifi_client_interface "
                "and roles.wan"
            )
        if not ssid:
            return ActionResult.failure("SSID must not be empty")
        if self.runtime.runner.which("wpa_supplicant") is None:
            return ActionResult.failure("wpa_supplicant is not installed")
        config_path = (
            Path("/etc/eventhub") / f"wpa_supplicant-{interface}.conf"
        )
        content = render_template(
            "modules/network/wpa_supplicant.conf.j2",
            ssid=ssid,
            password=password,
            interface=interface,
        )
        self.runtime.fs.atomic_write(config_path, content, mode=0o600)
        link = self.runtime.runner.run(["ip", "link", "set", interface, "up"], timeout=15)
        if not link.ok:
            return ActionResult.failure(f"could not bring up {interface}: {link.stderr.strip()}")
        self.runtime.runner.run(["wpa_cli", "-i", interface, "terminate"], timeout=10)
        start = self.runtime.runner.run(
            [
                "wpa_supplicant",
                "-B",
                "-i",
                interface,
                "-c",
                str(config_path),
                "-D",
                "nl80211,wext",
            ],
            timeout=30,
        )
        if not start.ok:
            return ActionResult.failure(
                f"wpa_supplicant failed: {start.stderr.strip() or start.stdout.strip()}"
            )
        dhcp = self._dhcp(interface)
        if not dhcp.ok:
            return dhcp
        self.runtime.bus.emit(
            "network.wifi_connected",
            {"interface": interface, "ssid": ssid},
            source="network",
        )
        return ActionResult.success(f"connected {interface} to {ssid}")

    def _dhcp(self, interface: str) -> ActionResult:
        if self.runtime.runner.which("dhclient") is not None:
            result = self.runtime.runner.run(
                ["dhclient", "-1", "-v", interface], timeout=45
            )
            if result.ok:
                return ActionResult.success("DHCP lease acquired")
        if self.runtime.runner.which("udhcpc") is not None:
            result = self.runtime.runner.run(
                ["udhcpc", "-i", interface, "-n", "-q"], timeout=45
            )
            if result.ok:
                return ActionResult.success("DHCP lease acquired")
        return ActionResult.failure("could not obtain a DHCP lease on the wifi uplink")

    def disconnect(self) -> ActionResult:
        interface = self.wifi_interface()
        if not interface:
            return ActionResult.failure("no wifi client interface")
        self.runtime.runner.run(["wpa_cli", "-i", interface, "terminate"], timeout=10)
        if self.runtime.runner.which("dhclient") is not None:
            self.runtime.runner.run(["dhclient", "-r", interface], timeout=15)
        return ActionResult.success(f"disconnected {interface}")

    def remember_network(self, ssid: str, password: str) -> list[WifiCredential]:
        networks = [
            item
            for item in self.config.network.wifi_networks
            if item.ssid != ssid
        ]
        networks.append(WifiCredential(ssid=ssid, password=password, priority=10))
        return networks

    # -- rendering -----------------------------------------------------
    def firewall_context(self) -> dict[str, object]:
        roles = self.roles()
        sharing = bool(
            self.config.network.internet_sharing
            and self.config.network.uplink != "none"
            and roles.wan
        )
        return {
            "table": self.config.network.nft_table,
            "ap": roles.ap,
            "lan": roles.lan,
            "wan": roles.wan,
            "sharing": sharing,
            "subnet": self.config.hotspot.subnet,
            "copyparty_port": self.config.fileserver_copyparty.port,
            "dashboard_port": self.config.dashboard.port,
            "smb_enabled": self.config.fileserver_smb.enabled,
            "copyparty_enabled": self.config.fileserver_copyparty.enabled,
            "dashboard_enabled": self.config.dashboard.enabled,
        }

    def sysctl_context(self) -> dict[str, object]:
        roles = self.roles()
        return {
            "sharing": bool(
                self.config.network.internet_sharing
                and self.config.network.uplink != "none"
                and roles.wan
            )
        }


def parse_uplink_json(text: str) -> UplinkState | None:
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    return UplinkState.from_dict(payload)
