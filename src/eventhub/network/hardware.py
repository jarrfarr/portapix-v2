"""Hardware discovery for the installer, wizard and network module.

Reads sysfs through the injected filesystem so it is fully testable.  Never
hardcodes ``wlan0``/``eth0``; returns concrete names plus a role suggestion.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eventhub.core.context import Runtime
from eventhub.core.types import DriveInfo, InterfaceInfo

# USB wifi chipsets known to work in monitor mode with Linux; the list is a
# hint, `iw list` is authoritative when it is available.
MONITOR_CAPABLE_DRIVERS = {
    "mt76x0u",
    "mt76x2u",
    "mt7601u",
    "mt7610u",
    "mt7921u",
    "mt7922u",
    "ath9k",
    "ath9k_htc",
    "carl9170",
    "rt2800usb",
    "rtl8187",
    "rtl8xxxu",
    "rtl88xxau",
    "rtl8812au",
    "rtl8814au",
    "88XXau",
    "rtw88_8822bu",
    "rtw88_8812au",
}

BUILTIN_WIFI_DRIVERS = {"brcmfmac", "brcmfmac_sdio", "brcmfmac_wcc"}
BUILTIN_ETHERNET_DRIVERS = {"bcmgenet", "smsc95xx", "lan78xx", "genet"}


@dataclass(frozen=True, slots=True)
class HardwareReport:
    is_raspberry_pi: bool = False
    model: str = "unknown"
    generation: str = ""
    interfaces: tuple[InterfaceInfo, ...] = ()
    drives: tuple[DriveInfo, ...] = ()
    airdrop_adapter: str | None = None
    airdrop_supported: bool = False
    suggested_roles: dict[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    @property
    def wifi_interfaces(self) -> tuple[InterfaceInfo, ...]:
        return tuple(i for i in self.interfaces if i.kind == "wifi")

    @property
    def ethernet_interfaces(self) -> tuple[InterfaceInfo, ...]:
        return tuple(i for i in self.interfaces if i.kind == "ethernet")

    def as_dict(self) -> dict[str, Any]:
        return {
            "is_raspberry_pi": self.is_raspberry_pi,
            "model": self.model,
            "generation": self.generation,
            "airdrop_adapter": self.airdrop_adapter,
            "airdrop_supported": self.airdrop_supported,
            "suggested_roles": dict(self.suggested_roles),
            "warnings": list(self.warnings),
            "interfaces": [
                {
                    "name": i.name,
                    "mac": i.mac,
                    "driver": i.driver,
                    "usb_id": i.usb_id,
                    "kind": i.kind,
                    "state": i.state,
                    "addresses": list(i.addresses),
                    "supports_ap": i.supports_ap,
                    "supports_monitor": i.supports_monitor,
                    "is_builtin": i.is_builtin,
                    "detail": i.detail,
                }
                for i in self.interfaces
            ],
            "drives": [
                {
                    "device": str(d.device),
                    "uuid": d.uuid,
                    "label": d.label,
                    "fstype": d.fstype,
                    "size_bytes": d.size_bytes,
                    "free_bytes": d.free_bytes,
                    "mounted": d.mounted,
                    "mount_point": str(d.mount_point) if d.mount_point else None,
                    "removable": d.removable,
                    "model": d.model,
                    "detail": d.detail,
                }
                for d in self.drives
            ],
        }


def _read(fs: Any, path: Path) -> str:
    try:
        return fs.read_text(path).strip().replace("\x00", "")
    except (OSError, FileNotFoundError):
        return ""


def _link(fs: Any, path: Path) -> str:
    try:
        return str(fs.readlink(path))
    except (OSError, FileNotFoundError):
        return ""


def _detect_model(fs: Any) -> tuple[bool, str, str]:
    for path in (
        Path("/sys/firmware/devicetree/base/model"),
        Path("/proc/device-tree/model"),
    ):
        model = _read(fs, path)
        if model:
            match = re.search(r"Raspberry Pi(?: Compute Module)?\s*(\d+)", model, re.I)
            generation = f"pi{match.group(1)}" if match else ""
            return "raspberry pi" in model.lower(), model, generation
    return False, "unknown", ""


def _iw_modes(runtime: Runtime) -> dict[str, set[str]]:
    """Return interface -> supported modes from `iw list` when available."""

    if runtime.runner.which("iw") is None:
        return {}
    result = runtime.runner.run(["iw", "list"], timeout=10)
    if not result.ok:
        return {}
    modes: dict[str, set[str]] = {}
    current_phy = ""
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Wiphy "):
            current_phy = stripped.split()[1]
            modes.setdefault(current_phy, set())
        elif stripped.startswith("* ") and current_phy:
            mode = stripped[2:].strip().lower()
            if mode:
                modes[current_phy].add(mode)
    return modes


def _ip_addresses(runtime: Runtime, name: str) -> tuple[str, ...]:
    if runtime.runner.which("ip") is None:
        return ()
    result = runtime.runner.run(["ip", "-j", "addr", "show", "dev", name], timeout=5)
    if not result.ok or not result.stdout.strip():
        return ()
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return ()
    addresses: list[str] = []
    for item in payload:
        for info in item.get("addr_info", []):
            local = info.get("local")
            if local:
                addresses.append(str(local))
    return tuple(addresses)


def _detect_interfaces(runtime: Runtime) -> tuple[InterfaceInfo, ...]:
    fs = runtime.fs
    base = Path("/sys/class/net")
    if not fs.exists(base):
        return ()
    try:
        names = fs.listdir(base)
    except OSError:
        return ()
    modes = _iw_modes(runtime)
    result: list[InterfaceInfo] = []
    for name in names:
        if name == "lo":
            continue
        path = base / name
        address = _read(fs, path / "address").lower()
        state = _read(fs, path / "operstate") or "unknown"
        raw_type = _read(fs, path / "type")
        driver = Path(_link(fs, path / "device" / "driver")).name
        device_link = _link(fs, path / "device")
        uevent = _read(fs, path / "device" / "uevent")
        usb_id = ""
        for line in uevent.splitlines():
            if line.startswith("PRODUCT=") or line.startswith("PCI_ID="):
                usb_id = line.split("=", 1)[1]
                break
        is_wireless = (
            raw_type == "801"
            or fs.exists(path / "wireless")
            or fs.exists(path / "phy80211")
            or driver in BUILTIN_WIFI_DRIVERS
        )
        kind = "wifi" if is_wireless else "ethernet" if raw_type == "1" else "other"
        is_usb = "usb" in device_link.lower() or driver in {
            "mt76x0u",
            "mt76x2u",
            "mt7601u",
            "ath9k_htc",
            "rt2800usb",
            "rtl8xxxu",
            "rtl88xxau",
            "rtl8812au",
            "cdc_ether",
            "r8152",
            "ax88179_178a",
        }
        is_builtin = not is_usb and (
            driver in BUILTIN_WIFI_DRIVERS | BUILTIN_ETHERNET_DRIVERS or not device_link
        )
        phy = _read(fs, path / "phy80211" / "name")
        supported_modes = modes.get(phy, set()) if phy else set()
        supports_ap = kind == "wifi" and (
            "ap" in supported_modes or is_builtin or driver not in MONITOR_CAPABLE_DRIVERS
        )
        supports_monitor = kind == "wifi" and (
            "monitor" in supported_modes or driver in MONITOR_CAPABLE_DRIVERS
        )
        result.append(
            InterfaceInfo(
                name=name,
                mac=address,
                driver=driver,
                usb_id=usb_id,
                kind=kind,  # type: ignore[arg-type]
                state=state,
                addresses=_ip_addresses(runtime, name),
                supports_ap=supports_ap,
                supports_monitor=supports_monitor,
                is_builtin=is_builtin,
                detail=(
                    f"{kind} {'usb' if is_usb else 'builtin'}"
                    f"{f', driver {driver}' if driver else ''}"
                ),
            )
        )
    return tuple(sorted(result, key=lambda item: item.name))


def _detect_drives(runtime: Runtime) -> tuple[DriveInfo, ...]:
    if runtime.runner.which("lsblk") is None:
        return ()
    result = runtime.runner.run(
        [
            "lsblk",
            "-J",
            "-b",
            "-o",
            "NAME,UUID,LABEL,FSTYPE,SIZE,MOUNTPOINT,TYPE,RM,MODEL",
        ],
        timeout=15,
    )
    if not result.ok or not result.stdout.strip():
        return ()
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return ()
    drives: list[DriveInfo] = []
    for block in payload.get("blockdevices", []):
        candidates = block.get("children") or [block]
        for item in candidates:
            fstype = str(item.get("fstype") or "")
            if not fstype:
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            mount = item.get("mountpoint")
            size = int(item.get("size") or 0)
            drives.append(
                DriveInfo(
                    device=Path("/dev") / name,
                    uuid=str(item.get("uuid") or ""),
                    label=str(item.get("label") or ""),
                    fstype=fstype,
                    size_bytes=size,
                    mounted=bool(mount),
                    mount_point=Path(str(mount)) if mount else None,
                    removable=bool(item.get("rm") or block.get("rm")),
                    model=str(item.get("model") or block.get("model") or ""),
                    detail=f"{fstype} {size // (1024**3)} GiB",
                )
            )
    return tuple(drives)


def detect_hardware(runtime: Runtime) -> HardwareReport:
    is_pi, model, generation = _detect_model(runtime.fs)
    interfaces = _detect_interfaces(runtime)
    drives = _detect_drives(runtime)

    wifi = [i for i in interfaces if i.kind == "wifi"]
    ethernet = [i for i in interfaces if i.kind == "ethernet"]
    onboard_wifi = next((i for i in wifi if i.is_builtin), None) or (wifi[0] if wifi else None)
    onboard_eth = next((i for i in ethernet if i.is_builtin), None)
    usb_eth = next((i for i in ethernet if not i.is_builtin), None)
    usb_wifi_monitor = next((i for i in wifi if i.supports_monitor and not i.is_builtin), None)

    warnings: list[str] = []
    if not is_pi:
        warnings.append(
            "not running on a Raspberry Pi; services will be configured but hardware "
            "checks cannot be trusted"
        )
    if not wifi:
        warnings.append("no wifi interface found; the hotspot cannot start")
    if usb_wifi_monitor is None:
        warnings.append(
            "no USB wifi adapter with monitor-mode support found; AirDrop will be "
            "disabled unless a compatible adapter is attached"
        )
    if usb_eth is None:
        warnings.append(
            "no USB ethernet dongle found; the WAN uplink will use wifi client mode "
            "or stay offline"
        )
    if not drives:
        warnings.append("no removable storage with a filesystem was detected")

    suggested = {
        "ap": onboard_wifi.name if onboard_wifi else "auto",
        "airdrop": usb_wifi_monitor.name if usb_wifi_monitor else "none",
        "wan": usb_eth.name if usb_eth else "auto",
        "lan": onboard_eth.name if onboard_eth else "auto",
    }
    return HardwareReport(
        is_raspberry_pi=is_pi,
        model=model,
        generation=generation,
        interfaces=interfaces,
        drives=drives,
        airdrop_adapter=usb_wifi_monitor.name if usb_wifi_monitor else None,
        airdrop_supported=usb_wifi_monitor is not None,
        suggested_roles=suggested,
        warnings=tuple(warnings),
    )
