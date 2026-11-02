from __future__ import annotations

from pathlib import Path

from eventhub.airdrop.service import AirdropService
from eventhub.core.config import AirdropConfig, DiscoveryConfig
from eventhub.core.types import CommandResult
from eventhub.discovery.service import DiscoveryService
from eventhub.installer.airdrop_build import install_owly
from tests.support import FakeFileSystem, FakeRunner, FakeSystemd, make_config, make_runtime


def airdrop_runtime(tmp_path: Path, *, owl: bool = True, systemd: FakeSystemd | None = None):
    config = make_config(tmp_path)
    fs = FakeFileSystem()
    if owl:
        fs.atomic_write("/usr/bin/owly", "#!/bin/sh\n")
        fs.atomic_write("/usr/lib/owly/owlyd", "#!/bin/sh\n")
    fs.mkdir("/sys/class/net/awdl0")
    runner = FakeRunner(
        binaries={"systemctl", "owly", "git", "cargo", "sh"},
    )
    return make_runtime(
        tmp_path, config=config, fs=fs, runner=runner, systemd=systemd or FakeSystemd()
    )


def test_airdrop_render_radio_config(tmp_path: Path) -> None:
    runtime = airdrop_runtime(tmp_path)
    files = AirdropService(runtime).render(runtime.config)
    assert len(files) == 1
    content = files[0].content
    assert 'station = "wlx-test"' in content
    assert "allow_untested = false" in content
    assert 'country = "GB"' in content


def test_airdrop_target_follows_active_event(tmp_path: Path) -> None:
    runtime = airdrop_runtime(tmp_path)
    from eventhub.events.service import EventsService

    event = EventsService(runtime).create_event("Shoot", event_date="2026-01-01", activate=True)
    target = AirdropService(runtime).target_dir()
    assert target == event.path / "ingest" / "anonymous"


def test_airdrop_target_falls_back_without_active_event(tmp_path: Path) -> None:
    runtime = airdrop_runtime(tmp_path)
    assert AirdropService(runtime).target_dir() == runtime.config.airdrop.target_dir


def test_airdrop_receive_argv(tmp_path: Path) -> None:
    runtime = airdrop_runtime(tmp_path)
    argv = AirdropService(runtime).receive_argv()
    assert argv[0].endswith("/owly")
    assert argv[1] == "receive"
    assert "--accept" in argv
    assert "--iface" in argv
    assert "--dir" in argv
    assert str(AirdropService(runtime).target_dir()) in argv


def test_airdrop_validate_contacts_warning(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        airdrop=AirdropConfig(
            enabled=True,
            receive_mode="contacts",
            target_dir=tmp_path / "inbox",
            owl_dir=tmp_path / "owl",
            owl_binary=tmp_path / "owl" / "owly",
            owl_daemon=tmp_path / "owl" / "owlyd",
        ),
    )
    runtime = make_runtime(tmp_path, config=config)
    issues = AirdropService(runtime).validate(config)
    assert any("Everyone" in issue.message for issue in issues)


def test_airdrop_apply_and_health(tmp_path: Path) -> None:
    systemd = FakeSystemd()
    runtime = airdrop_runtime(tmp_path, systemd=systemd)
    service = AirdropService(runtime)
    result = service.apply()
    assert result.ok, result.message
    assert ("start", "owlyd.service") in systemd.actions
    assert ("restart", "eventhub-radio.service") in systemd.actions
    assert ("restart", "eventhub-airdrop.service") in systemd.actions
    health = service.health_check()
    assert health.healthy is True, health.message


def test_airdrop_stop(tmp_path: Path) -> None:
    systemd = FakeSystemd(
        active={"eventhub-radio.service", "eventhub-airdrop.service"}
    )
    runtime = airdrop_runtime(tmp_path, systemd=systemd)
    result = AirdropService(runtime).stop()
    assert result.ok
    assert ("stop", "eventhub-airdrop.service") in systemd.actions
    assert ("stop", "eventhub-radio.service") in systemd.actions


def test_airdrop_doctor_parses_json(tmp_path: Path) -> None:
    runtime = airdrop_runtime(tmp_path)
    runtime.runner.responses["/usr/bin/owly doctor"] = CommandResult(
        ("owly",), 0, stdout='{"ok": true, "card": "mt76x0u"}'
    )
    payload = AirdropService(runtime).doctor()
    assert payload["card"] == "mt76x0u"


def test_airdrop_missing_binary_validate(tmp_path: Path) -> None:
    runtime = airdrop_runtime(tmp_path, owl=False)
    runtime.runner.binaries.discard("owly")
    issues = AirdropService(runtime).validate(runtime.config)
    assert any("owly" in issue.message for issue in issues)


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def test_discovery_render() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        tmp_path = Path(directory)
        config = make_config(
            tmp_path,
            discovery=DiscoveryConfig(
                enabled=True,
                hostname="eventhub",
                advertise_smb=True,
                advertise_copyparty=True,
                advertise_dashboard=True,
            ),
        )
        runtime = make_runtime(tmp_path, config=config)
        files = {str(item.path): item.content for item in DiscoveryService(runtime).render(config)}
        assert "/etc/avahi/avahi-daemon.conf" in files
        assert "allow-interfaces=wlan-test,eth-lan" in files["/etc/avahi/avahi-daemon.conf"]
        assert "_smb._tcp" in files["/etc/avahi/services/eventhub-smb.service"]
        assert "3923" in files["/etc/avahi/services/eventhub-copyparty.service"]
        assert "80" in files["/etc/avahi/services/eventhub-dashboard.service"]


def test_discovery_apply_and_health(tmp_path: Path) -> None:
    runner = FakeRunner(binaries={"systemctl", "avahi-resolve", "hostnamectl", "avahi-daemon"})
    runner.responses["avahi-resolve"] = CommandResult(
        ("avahi-resolve",), 0, stdout="eventhub.local 10.42.0.1\n"
    )
    systemd = FakeSystemd(active={"avahi-daemon.service"})
    runtime = make_runtime(tmp_path, runner=runner, systemd=systemd)
    service = DiscoveryService(runtime)
    result = service.apply()
    assert result.ok, result.message
    assert runtime.runner.called("hostnamectl set-hostname eventhub") or runtime.runner.called(
        "hostnamectl set-hostname testhub"
    )
    assert service.health_check().healthy is True


def test_discovery_removes_disabled_service_file(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        discovery=DiscoveryConfig(
            enabled=True,
            hostname="eventhub",
            advertise_smb=False,
        ),
    )
    fs = FakeFileSystem()
    fs.atomic_write("/etc/avahi/services/eventhub-smb.service", "stale")
    systemd = FakeSystemd(active={"avahi-daemon.service"})
    runtime = make_runtime(tmp_path, config=config, fs=fs, systemd=systemd)
    result = DiscoveryService(runtime).apply()
    assert result.ok
    assert not fs.exists("/etc/avahi/services/eventhub-smb.service")


# ---------------------------------------------------------------------------
# installer build step
# ---------------------------------------------------------------------------


def test_install_owly_skips_existing(tmp_path: Path) -> None:
    runtime = airdrop_runtime(tmp_path)
    result = install_owly(runtime, runtime.config.airdrop)
    assert result.ok
    assert "already" in result.message


def test_install_owly_without_cargo_disables(tmp_path: Path) -> None:
    runtime = airdrop_runtime(tmp_path, owl=False)
    runtime.runner.binaries.discard("cargo")
    result = install_owly(runtime, runtime.config.airdrop)
    assert not result.ok
    assert result.disabled
    assert "Rust" in result.message


def test_install_owly_old_cargo_disables(tmp_path: Path) -> None:
    runtime = airdrop_runtime(tmp_path, owl=False)
    runtime.runner.responses["*cargo --version*"] = CommandResult(
        ("cargo",), 0, stdout="cargo 1.70.0\n"
    )
    result = install_owly(runtime, runtime.config.airdrop)
    assert result.disabled
    assert "older" in result.message


def test_install_owly_builds_from_source(tmp_path: Path) -> None:
    runtime = airdrop_runtime(tmp_path, owl=False)
    runtime.runner.responses["*cargo --version*"] = CommandResult(
        ("cargo",), 0, stdout="cargo 1.90.0\n"
    )
    result = install_owly(runtime, runtime.config.airdrop)
    assert result.ok, result.message
    assert runtime.runner.called("git clone")
    assert runtime.runner.called("cargo build --release --locked")
    assert runtime.runner.called("packaging/install.sh owly")


def test_install_owly_build_failure_disables(tmp_path: Path) -> None:
    runtime = airdrop_runtime(tmp_path, owl=False)
    runtime.runner.responses["*cargo --version*"] = CommandResult(
        ("cargo",), 0, stdout="cargo 1.90.0\n"
    )
    runtime.runner.responses["*cargo build*"] = CommandResult(
        ("cargo",), 1, stderr="compile error"
    )
    result = install_owly(runtime, runtime.config.airdrop)
    assert not result.ok
    assert result.disabled
    assert "compile error" in result.message
