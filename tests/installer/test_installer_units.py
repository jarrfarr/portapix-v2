from __future__ import annotations

from pathlib import Path

import pytest

from eventhub.core.templates import templates_dir
from eventhub.core.types import CommandResult
from eventhub.installer.distro import (
    PackageManager,
    detect_distro,
    package_commands,
    resolve_packages,
)
from eventhub.installer.units import render_units, unit_names
from eventhub.network.hardware import detect_hardware
from tests.support import FakeFileSystem, FakeRunner, make_config, make_runtime


def populate_distro(fs: FakeFileSystem, distro: str = "debian") -> None:
    fs.write_text(
        "/etc/os-release",
        f'ID={distro}\nPRETTY_NAME="Test {distro}"\nVERSION_ID="12"\n',
    )


def test_detect_debian() -> None:
    fs = FakeFileSystem()
    populate_distro(fs)
    distro = detect_distro(fs)
    assert distro.package_manager is PackageManager.APT
    assert distro.family == "debian"
    assert distro.supported


def test_detect_raspberry_pi_os() -> None:
    fs = FakeFileSystem()
    fs.write_text("/etc/os-release", 'ID=raspbian\nPRETTY_NAME="Raspberry Pi OS"\n')
    distro = detect_distro(fs)
    assert distro.raspberry_pi_os
    assert distro.package_manager is PackageManager.APT


def test_detect_unknown_distro() -> None:
    fs = FakeFileSystem()
    fs.write_text("/etc/os-release", 'ID=plan9\nPRETTY_NAME="Plan 9"\n')
    distro = detect_distro(fs)
    assert distro.package_manager is PackageManager.UNKNOWN
    assert not distro.supported


def test_debian_package_command() -> None:
    commands = package_commands(PackageManager.APT, ["hostapd", "python3-venv"], "debian")
    assert len(commands) == 2
    assert commands[0].argv[:2] == ("apt-get", "update")
    assert "hostapd" in commands[1].argv
    assert "python3-venv" in commands[1].argv
    assert commands[1].env == {"DEBIAN_FRONTEND": "noninteractive"}


def test_package_name_translation() -> None:
    assert resolve_packages(["build-essential"], "fedora") == ["gcc", "gcc-c++", "make"]
    assert resolve_packages(["samba-common-bin"], "arch") == []
    assert resolve_packages(["nftables"], "unknown") == ["nftables"]


def _populate_pi(fs: FakeFileSystem) -> None:
    fs.write_text("/sys/firmware/devicetree/base/model", "Raspberry Pi 4 Model B Rev 1.4\x00")
    # onboard wifi
    fs.mkdir("/sys/class/net/wlan0")
    fs.write_text("/sys/class/net/wlan0/address", "aa:bb:cc:dd:ee:01")
    fs.write_text("/sys/class/net/wlan0/type", "801")
    fs.write_text("/sys/class/net/wlan0/operstate", "up")
    fs.symlink("/sys/devices/platform/brcmfmac", "/sys/class/net/wlan0/device")
    fs.symlink("/sys/bus/platform/drivers/brcmfmac", "/sys/class/net/wlan0/device/driver")
    fs.mkdir("/sys/class/net/wlan0/wireless")
    # usb monitor wifi
    fs.mkdir("/sys/class/net/wlx1")
    fs.write_text("/sys/class/net/wlx1/address", "aa:bb:cc:dd:ee:02")
    fs.write_text("/sys/class/net/wlx1/type", "801")
    fs.write_text("/sys/class/net/wlx1/operstate", "down")
    fs.symlink("/sys/devices/usb1/1-1/1-1:1.0", "/sys/class/net/wlx1/device")
    fs.symlink("/sys/bus/usb/drivers/mt76x0u", "/sys/class/net/wlx1/device/driver")
    fs.write_text("/sys/class/net/wlx1/device/uevent", "PRODUCT=0bda/8812/200\n")
    # onboard ethernet
    fs.mkdir("/sys/class/net/eth0")
    fs.write_text("/sys/class/net/eth0/address", "aa:bb:cc:dd:ee:03")
    fs.write_text("/sys/class/net/eth0/type", "1")
    fs.write_text("/sys/class/net/eth0/operstate", "up")
    fs.symlink("/sys/devices/platform/genet", "/sys/class/net/eth0/device")
    fs.symlink("/sys/bus/platform/drivers/bcmgenet", "/sys/class/net/eth0/device/driver")
    # usb ethernet
    fs.mkdir("/sys/class/net/eth1")
    fs.write_text("/sys/class/net/eth1/address", "aa:bb:cc:dd:ee:04")
    fs.write_text("/sys/class/net/eth1/type", "1")
    fs.write_text("/sys/class/net/eth1/operstate", "down")
    fs.symlink("/sys/devices/usb1/1-2/1-2:1.0", "/sys/class/net/eth1/device")
    fs.symlink("/sys/bus/usb/drivers/r8152", "/sys/class/net/eth1/device/driver")


def test_hardware_detection_on_pi(tmp_path: Path) -> None:
    fs = FakeFileSystem()
    _populate_pi(fs)
    runner = FakeRunner(
        binaries={"systemctl", "lsblk", "ip"},
        responses={
            "lsblk": CommandResult(
                argv=("lsblk",),
                returncode=0,
                stdout=(
                    '{"blockdevices":[{"name":"sda","rm":true,"model":"SSD",'
                    '"children":[{"name":"sda1","uuid":"1234-ABCD","label":"EVENTS",'
                    '"fstype":"exfat","size":1000000000,"mountpoint":null,"rm":true}]}]}'
                ),
            )
        }
    )
    runtime = make_runtime(tmp_path, fs=fs, runner=runner)
    report = detect_hardware(runtime)
    assert report.is_raspberry_pi
    assert report.model.startswith("Raspberry Pi 4")
    names = {i.name for i in report.interfaces}
    assert names == {"wlan0", "wlx1", "eth0", "eth1"}
    assert report.suggested_roles["ap"] == "wlan0"
    assert report.suggested_roles["airdrop"] == "wlx1"
    assert report.suggested_roles["wan"] == "eth1"
    assert report.suggested_roles["lan"] == "eth0"
    assert report.airdrop_supported
    assert len(report.drives) == 1
    assert report.drives[0].uuid == "1234-ABCD"


def test_hardware_reports_unsupported_airdrop(tmp_path: Path) -> None:
    fs = FakeFileSystem()
    fs.write_text("/sys/firmware/devicetree/base/model", "Raspberry Pi 5 Model B")
    fs.mkdir("/sys/class/net/wlan0")
    fs.write_text("/sys/class/net/wlan0/type", "801")
    fs.symlink("/sys/devices/platform/brcmfmac", "/sys/class/net/wlan0/device")
    fs.symlink("/sys/bus/platform/drivers/brcmfmac", "/sys/class/net/wlan0/device/driver")
    runtime = make_runtime(tmp_path, fs=fs)
    report = detect_hardware(runtime)
    assert not report.airdrop_supported
    assert any("AirDrop" in warning for warning in report.warnings)


def test_render_all_units(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    modules = [
        "helper",
        "dashboard",
        "fileserver_copyparty",
        "airdrop",
        "network",
        "hotspot",
        "system",
    ]
    rendered = render_units(config, modules)
    assert len(rendered) == len(modules) + 2  # hotspot and airdrop each have two units
    for item in rendered:
        assert item.path.parent == Path("/etc/systemd/system")
        assert "{{" not in item.content
        assert "}}" not in item.content
        assert "eventhub" in item.content
    assert templates_dir() is not None
    names = unit_names(modules)
    assert "eventhub-helper.service" in names
    assert "eventhub-hostapd.service" in names
    assert "eventhub-dnsmasq.service" in names


def test_unit_names_arch_smb(tmp_path: Path) -> None:
    from eventhub.installer.distro import Distro

    arch = Distro(family="arch", package_manager=PackageManager.PACMAN, supported=True)
    names = unit_names(["fileserver_smb"], arch)
    assert names == ["smb.service"]


@pytest.mark.parametrize("module", ["helper", "dashboard"])
def test_unit_templates_have_correct_option_order(tmp_path: Path, module: str) -> None:
    config = make_config(tmp_path)
    item = render_units(config, [module])[0]
    assert "eventhub --config" in item.content
    assert "eventhub serve --config" not in item.content
