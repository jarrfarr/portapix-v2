"""Distro and package-manager detection with best-effort mapping."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from eventhub.core.adapters import FileSystem


class PackageManager(StrEnum):
    APT = "apt"
    DNF = "dnf"
    PACMAN = "pacman"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Distro:
    id: str = "unknown"
    name: str = "Unknown"
    version: str = ""
    family: str = "unknown"
    package_manager: PackageManager = PackageManager.UNKNOWN
    supported: bool = False
    raspberry_pi_os: bool = False
    notes: tuple[str, ...] = ()

    @property
    def is_debian(self) -> bool:
        return self.family == "debian"


def _parse_os_release(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        values[key.strip()] = raw.strip().strip('"').strip("'")
    return values


def detect_distro(fs: FileSystem) -> Distro:
    try:
        text = fs.read_text("/etc/os-release")
    except (OSError, FileNotFoundError):
        return Distro(notes=("could not read /etc/os-release",))
    values = _parse_os_release(text)
    distro_id = values.get("ID", "unknown").lower()
    like = values.get("ID_LIKE", "").lower()
    name = values.get("PRETTY_NAME") or values.get("NAME", "Unknown")
    version = values.get("VERSION_ID", "")
    pi_os = distro_id in {"raspbian", "raspberrypi"} or "raspbian" in like

    manager = PackageManager.UNKNOWN
    if distro_id in {"debian", "ubuntu", "raspbian", "raspberrypi", "linuxmint", "pop"} or (
        "debian" in like or "ubuntu" in like
    ):
        manager = PackageManager.APT
    elif distro_id in {"fedora", "rhel", "centos", "rocky", "almalinux"} or "fedora" in like:
        manager = PackageManager.DNF
    elif distro_id in {"arch", "manjaro", "endeavouros"} or "arch" in like:
        manager = PackageManager.PACMAN

    notes: list[str] = []
    if pi_os:
        notes.append("Raspberry Pi OS detected")
    if manager is PackageManager.DNF:
        notes.append("Fedora/RHEL support is best-effort; package names may differ")
    if manager is PackageManager.PACMAN:
        notes.append("Arch support is best-effort; package names may differ")
    if manager is PackageManager.UNKNOWN:
        notes.append("unsupported distribution: no known package manager")

    return Distro(
        id=distro_id,
        name=name,
        version=version,
        family={
            PackageManager.APT: "debian",
            PackageManager.DNF: "fedora",
            PackageManager.PACMAN: "arch",
        }.get(manager, "unknown"),
        package_manager=manager,
        supported=manager is not PackageManager.UNKNOWN,
        raspberry_pi_os=pi_os,
        notes=tuple(notes),
    )


# Logical package -> per-family package names.  A logical package that is
# absent from the mapping is used verbatim on every family.
PACKAGE_MAP: dict[str, dict[str, list[str]]] = {
    "python3-venv": {
        "apt": ["python3-venv"],
        "fedora": ["python3"],
        "arch": ["python"],
    },
    "python3-pip": {
        "apt": ["python3-pip"],
        "fedora": ["python3-pip"],
        "arch": ["python-pip"],
    },
    "whiptail": {
        "apt": ["whiptail"],
        "fedora": ["newt"],
        "arch": ["libnewt"],
    },
    "wpasupplicant": {
        "apt": ["wpasupplicant"],
        "fedora": ["wpa_supplicant"],
        "arch": ["wpa_supplicant"],
    },
    "samba-common-bin": {
        "apt": ["samba-common-bin"],
        "fedora": [],
        "arch": [],
    },
    "libnss-mdns": {
        "apt": ["libnss-mdns"],
        "fedora": ["nss-mdns"],
        "arch": ["nss-mdns"],
    },
    "build-essential": {
        "apt": ["build-essential"],
        "fedora": ["gcc", "gcc-c++", "make"],
        "arch": ["base-devel"],
    },
    "libnl-3-dev": {
        "apt": ["libnl-3-dev", "libnl-genl-3-dev"],
        "fedora": ["libnl3-devel"],
        "arch": ["libnl"],
    },
    "libpcap-dev": {
        "apt": ["libpcap-dev"],
        "fedora": ["libpcap-devel"],
        "arch": ["libpcap"],
    },
    "libssl-dev": {
        "apt": ["libssl-dev"],
        "fedora": ["openssl-devel"],
        "arch": ["openssl"],
    },
    "libusb-1.0-0-dev": {
        "apt": ["libusb-1.0-0-dev"],
        "fedora": ["libusb1-devel"],
        "arch": ["libusb"],
    },
    "libglib2.0-dev": {
        "apt": ["libglib2.0-dev"],
        "fedora": ["glib2-devel"],
        "arch": ["glib2"],
    },
    "libdbus-1-dev": {
        "apt": ["libdbus-1-dev"],
        "fedora": ["dbus-devel"],
        "arch": ["dbus"],
    },
    "libudev-dev": {
        "apt": ["libudev-dev"],
        "fedora": ["systemd-devel"],
        "arch": ["systemd-libs"],
    },
    "exfatprogs": {
        "apt": ["exfatprogs"],
        "fedora": ["exfatprogs"],
        "arch": ["exfatprogs"],
    },
    "wireless-regdb": {
        "apt": ["wireless-regdb"],
        "fedora": ["wireless-regdb"],
        "arch": ["wireless-regdb"],
    },
    "avahi-daemon": {
        "apt": ["avahi-daemon", "avahi-utils"],
        "fedora": ["avahi", "avahi-tools"],
        "arch": ["avahi"],
    },
    "nftables": {
        "apt": ["nftables"],
        "fedora": ["nftables"],
        "arch": ["nftables"],
    },
    "hostapd": {
        "apt": ["hostapd"],
        "fedora": ["hostapd"],
        "arch": ["hostapd"],
    },
}


@dataclass(frozen=True, slots=True)
class PackageCommand:
    argv: tuple[str, ...]
    description: str
    env: dict[str, str] | None = None


def resolve_packages(logical: list[str], family: str) -> list[str]:
    """Translate logical package names for a distro family, deduplicated."""

    resolved: list[str] = []
    seen: set[str] = set()
    for name in logical:
        candidates = PACKAGE_MAP.get(name, {}).get(family, [name])
        for candidate in candidates:
            if candidate and candidate not in seen:
                seen.add(candidate)
                resolved.append(candidate)
    return resolved


def package_commands(
    manager: PackageManager, logical: list[str], family: str
) -> list[PackageCommand]:
    packages = resolve_packages(logical, family)
    if not packages:
        return []
    if manager is PackageManager.APT:
        return [
            PackageCommand(("apt-get", "update"), "update apt package lists"),
            PackageCommand(
                (
                    "apt-get",
                    "install",
                    "-y",
                    "--no-install-recommends",
                    *packages,
                ),
                "install system packages",
                env={"DEBIAN_FRONTEND": "noninteractive"},
            ),
        ]
    if manager is PackageManager.DNF:
        return [
            PackageCommand(("dnf", "install", "-y", *packages), "install system packages")
        ]
    if manager is PackageManager.PACMAN:
        return [
            PackageCommand(
                ("pacman", "-S", "--noconfirm", "--needed", *packages),
                "install system packages",
            )
        ]
    return []


class CommandRunnerLike(Protocol):
    def run(
        self,
        argv: object,
        *,
        check: bool = False,
        timeout: float = 30.0,
        input_text: str | None = None,
        env: object = None,
        cwd: object = None,
    ) -> object: ...
