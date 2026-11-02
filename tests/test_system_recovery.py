from __future__ import annotations

from pathlib import Path

from eventhub.system.recovery import RECOVERY_SSID, RecoveryManager
from eventhub.system.selftest import SelfTest
from eventhub.system.service import SystemService, metrics_path
from eventhub.users.service import UsersService
from tests.support import FakeFileSystem, FakeRunner, FakeSystemd, make_config, make_runtime


def populate_system_files(fs: FakeFileSystem) -> None:
    fs.write_text("/proc/uptime", "7200.00 100.00\n")
    fs.write_text("/proc/loadavg", "0.10 0.20 0.30 1/100 1234\n")
    fs.write_text(
        "/proc/meminfo",
        "MemTotal:        1000000 kB\nMemAvailable:     400000 kB\n",
    )
    fs.mkdir("/sys/class/thermal/thermal_zone0")
    fs.write_text("/sys/class/thermal/thermal_zone0/temp", "48500\n")


def test_system_metrics_snapshot_and_write(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    fs = FakeFileSystem()
    populate_system_files(fs)
    systemd = FakeSystemd(active={"eventhub-metrics.service"})
    runtime = make_runtime(tmp_path, config=config, fs=fs, systemd=systemd)
    service = SystemService(runtime)
    metrics = service.snapshot()
    assert metrics["cpu_temp_c"] == 48.5
    assert metrics["uptime_seconds"] == 7200
    assert metrics["load"] == [0.1, 0.2, 0.3]
    assert metrics["memory"]["used_percent"] == 60.0
    written = service.write_metrics()
    assert written["cpu_temp_c"] == 48.5
    assert fs.exists(metrics_path(runtime))
    health = service.health_check()
    assert health.healthy is True


def test_system_render_journald(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    runtime = make_runtime(tmp_path, config=config)
    files = SystemService(runtime).render(config)
    assert len(files) == 1
    content = files[0].content
    assert "SystemMaxUse=" in content
    assert "MaxRetentionSec=" in content


def test_system_apply_and_timezone(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    runner = FakeRunner(binaries={"systemctl", "timedatectl"})
    systemd = FakeSystemd()
    runtime = make_runtime(tmp_path, config=config, runner=runner, systemd=systemd)
    service = SystemService(runtime)
    result = service.apply()
    assert result.ok, result.message
    assert ("restart", "systemd-journald.service") in systemd.actions
    assert service.set_timezone("Europe/London").ok
    assert not service.set_timezone("Not/AZone").ok
    assert not service.reboot().ok
    assert service.reboot(confirm=True).ok


def test_system_metrics_loop(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    fs = FakeFileSystem()
    populate_system_files(fs)
    runtime = make_runtime(tmp_path, config=config, fs=fs)
    service = SystemService(runtime)
    service.metrics_loop(iterations=1)
    assert fs.exists(metrics_path(runtime))


def test_recovery_reset_admin(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    UsersService(runtime).create_user(
        "admin", "old-password-123", role="admin", must_change_password=False
    )
    manager = RecoveryManager(runtime)
    result = manager.reset_admin()
    assert result.ok, result.message
    password = result.details["password"]
    assert UsersService(runtime).authenticate("admin", password) is not None
    assert runtime.fs.exists(runtime.config.dashboard.first_boot_password_file)
    assert "Password:" in runtime.fs.read_text(
        runtime.config.dashboard.first_boot_password_file
    )


def test_recovery_reset_hotspot(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    runner = FakeRunner(binaries={"systemctl", "nft", "hostapd", "dnsmasq", "iw", "rfkill"})
    systemd = FakeSystemd()
    runtime = make_runtime(tmp_path, config=config, runner=runner, systemd=systemd)
    result = RecoveryManager(runtime).reset_hotspot()
    assert result.ok, result.message
    assert runtime.config.hotspot.ssid == RECOVERY_SSID
    assert runtime.fs.exists(Path("/var/lib/eventhub/recovery-hotspot.txt"))


def test_recovery_boot_check_replaces_invalid_config(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    fs = FakeFileSystem()
    populate_system_files(fs)
    fs.write_text("/etc/os-release", 'ID=debian\nPRETTY_NAME="Debian"\n')
    # minimal Pi interfaces so the recovery hotspot can render
    fs.mkdir("/sys/class/net/wlan0")
    fs.write_text("/sys/class/net/wlan0/type", "801")
    fs.write_text("/sys/class/net/wlan0/address", "aa:bb:cc:dd:ee:01")
    fs.symlink("/sys/devices/platform/brcmfmac", "/sys/class/net/wlan0/device")
    fs.symlink("/sys/bus/platform/drivers/brcmfmac", "/sys/class/net/wlan0/device/driver")
    runner = FakeRunner(binaries={"systemctl", "nft", "hostapd", "dnsmasq", "iw", "rfkill"})
    runtime = make_runtime(tmp_path, config=config, fs=fs, runner=runner, systemd=FakeSystemd())
    result = RecoveryManager(runtime).boot_check()
    assert result.ok, result.message
    text = fs.read_text(runtime.config_path)
    assert RECOVERY_SSID in text
    assert fs.exists(Path("/var/lib/eventhub/recovery-hotspot.txt"))


def test_selftest_reports_checks(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    for section in (
        "storage",
        "network",
        "hotspot",
        "fileserver_smb",
        "fileserver_copyparty",
        "airdrop",
        "discovery",
        "dashboard",
        "helper",
        "system",
    ):
        getattr(config, section).enabled = False
    runtime = make_runtime(tmp_path, config=config)
    UsersService(runtime).create_user(
        "admin", "admin-password-123", role="admin", must_change_password=False
    )
    from eventhub.events.service import EventsService

    EventsService(runtime).create_event("Shoot", event_date="2026-01-01", activate=True)
    report = SelfTest(runtime).run()
    payload = report.as_dict()
    assert payload["checks"]
    assert payload["summary"]
    assert report.ok, payload
    assert any(check.name == "users" for check in report.checks)
