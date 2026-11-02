"""Logical system package sets, translated by :mod:`eventhub.installer.distro`."""

from __future__ import annotations

BASE_PACKAGES: list[str] = [
    "git",
    "curl",
    "ca-certificates",
    "python3",
    "python3-venv",
    "python3-pip",
    "rsync",
]

MODULE_PACKAGES: dict[str, list[str]] = {
    "network": ["nftables", "iproute2", "iw", "wpasupplicant", "ethtool", "rfkill"],
    "hotspot": ["hostapd", "dnsmasq", "iw", "wireless-regdb", "rfkill"],
    "storage": ["exfatprogs", "e2fsprogs", "dosfstools", "util-linux", "smartmontools"],
    "events": [],
    "users": [],
    "fileserver_smb": ["samba", "samba-common-bin"],
    "fileserver_copyparty": ["ffmpeg"],
    "airdrop": [
        "build-essential",
        "cmake",
        "pkg-config",
        "libnl-3-dev",
        "libpcap-dev",
        "libssl-dev",
        "libusb-1.0-0-dev",
        "libglib2.0-dev",
        "libdbus-1-dev",
        "libudev-dev",
        "hostapd",
        "iproute2",
        "iw",
    ],
    "discovery": ["avahi-daemon", "libnss-mdns"],
    "dashboard": [],
    "system": ["util-linux", "smartmontools"],
}


def packages_for_modules(modules: list[str], *, include_base: bool = True) -> list[str]:
    packages = list(BASE_PACKAGES) if include_base else []
    for module in modules:
        packages.extend(MODULE_PACKAGES.get(module, []))
    # stable dedupe
    return list(dict.fromkeys(packages))
