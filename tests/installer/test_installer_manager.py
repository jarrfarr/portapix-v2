from __future__ import annotations

from pathlib import Path

from eventhub.core.adapters import RealFileSystem
from eventhub.core.config import load_config, save_config
from eventhub.core.types import CommandResult
from eventhub.installer.manager import Installer
from tests.support import FakeFileSystem, FakeRunner, FakeSystemd, make_config, make_runtime


def populate_system(fs: FakeFileSystem, distro: str = "debian") -> None:
    fs.write_text(
        "/etc/os-release",
        f'ID={distro}\nPRETTY_NAME="Test {distro}"\nVERSION_ID="12"\n',
    )


def test_install_requires_root(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path, privileged=False)
    result = Installer(runtime).install()
    assert not result.ok
    assert "root" in result.message


def test_install_dry_run_changes_nothing(tmp_path: Path) -> None:
    fs = FakeFileSystem()
    populate_system(fs)
    runtime = make_runtime(tmp_path, fs=fs)
    result = Installer(runtime).install(dry_run=True)
    assert result.ok, result.message
    assert result.dry_run
    assert not fs.exists(runtime.config_path)
    assert not fs.exists("/etc/systemd/system/eventhub-helper.service")


def test_install_full_with_fakes(tmp_path: Path) -> None:
    fs = FakeFileSystem()
    populate_system(fs)
    systemd = FakeSystemd()
    runtime = make_runtime(tmp_path, fs=fs, systemd=systemd)
    result = Installer(runtime).install(unattended=True)
    assert result.ok, result.message
    assert result.admin_password
    # config is written with mode 0600
    assert fs.exists(runtime.config_path)
    assert fs.mode_of(runtime.config_path) == 0o600
    # admin row and password file
    assert runtime.db.scalar("SELECT COUNT(*) FROM users WHERE role='admin'") == 1
    password_file = runtime.config.dashboard.first_boot_password_file
    assert fs.exists(password_file)
    assert result.admin_password in fs.read_text(password_file)
    assert fs.mode_of(password_file) == 0o600
    # no modules exist yet, so no unit files are rendered
    assert not fs.exists("/etc/systemd/system/eventhub-helper.service")
    # package manager was invoked
    assert runtime.runner.called("apt-get update")
    assert runtime.runner.called("apt-get install")


def test_install_is_idempotent(tmp_path: Path) -> None:
    fs = FakeFileSystem()
    populate_system(fs)
    runtime = make_runtime(tmp_path, fs=fs)
    first = Installer(runtime).install(unattended=True)
    assert first.ok, first.message
    second = Installer(runtime).install(unattended=True)
    assert second.ok, second.message
    assert second.admin_password is None
    assert runtime.db.scalar("SELECT COUNT(*) FROM users") == 1
    assert runtime.runner.command_count("useradd") == 0


def test_install_unsupported_distro_fails(tmp_path: Path) -> None:
    fs = FakeFileSystem()
    fs.write_text("/etc/os-release", 'ID=plan9\nPRETTY_NAME="Plan 9"\n')
    runtime = make_runtime(tmp_path, fs=fs)
    result = Installer(runtime).install(unattended=True)
    assert not result.ok
    assert not fs.exists(runtime.config_path)


def test_install_skip_packages_allows_unknown_distro(tmp_path: Path) -> None:
    fs = FakeFileSystem()
    fs.write_text("/etc/os-release", 'ID=plan9\nPRETTY_NAME="Plan 9"\n')
    runtime = make_runtime(tmp_path, fs=fs)
    result = Installer(runtime).install(unattended=True, skip_packages=True)
    assert result.ok, result.message
    assert fs.exists(runtime.config_path)


def test_answers_file_merges(tmp_path: Path) -> None:
    fs = FakeFileSystem()
    populate_system(fs)
    answers = tmp_path / "answers.yaml"
    fs.write_text(
        answers,
        "hostname: shootbox\nhotspot:\n  ssid: ShootNet\n  password: answers-password\n",
    )
    runtime = make_runtime(tmp_path, fs=fs)
    result = Installer(runtime).install(unattended=True, answers_path=answers)
    assert result.ok, result.message
    text = fs.read_text(runtime.config_path)
    assert "ShootNet" in text
    assert "shootbox" in text


def test_update_check_only(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    (tmp_path / "opt" / ".git").mkdir(parents=True)
    save_config(config, tmp_path / "etc" / "config.yaml")
    runner = FakeRunner(
        responses={
            "git -C * rev-parse*": CommandResult(("git",), 0, stdout="a" * 40 + "\n"),
            "git -C * ls-remote*": CommandResult(
                ("git",), 0, stdout="b" * 40 + "\trefs/heads/main\n"
            ),
        }
    )
    runtime = make_runtime(
        tmp_path, runner=runner, fs=RealFileSystem(), privileged=True
    )
    result = Installer(runtime).update(check_only=True)
    assert result.ok
    assert "available" in result.message
    assert not (tmp_path / "state" / "backups").exists()


def test_uninstall_services_only_keeps_everything(tmp_path: Path) -> None:
    fs = FakeFileSystem()
    populate_system(fs)
    runtime = make_runtime(tmp_path, fs=fs)
    fs.atomic_write(runtime.config_path, "config", mode=0o600)
    fs.atomic_write(runtime.config.data_root / "events" / "x" / "file.jpg", "photo")
    result = Installer(runtime).uninstall(mode="services-only", yes=True)
    assert result.ok
    assert fs.exists(runtime.config_path)
    assert fs.exists(runtime.config.data_root / "events" / "x" / "file.jpg")


def test_uninstall_keep_data(tmp_path: Path) -> None:
    fs = FakeFileSystem()
    populate_system(fs)
    runtime = make_runtime(tmp_path, fs=fs)
    fs.atomic_write(runtime.config_path, "config", mode=0o600)
    fs.atomic_write(runtime.config.data_root / "events" / "x" / "file.jpg", "photo")
    result = Installer(runtime).uninstall(mode="keep-data", yes=True)
    assert result.ok
    assert not fs.exists(runtime.config_path)
    assert not fs.exists(runtime.config.state_dir)
    assert fs.exists(runtime.config.data_root / "events" / "x" / "file.jpg")


def test_uninstall_full_wipe_requires_typed_confirmation(tmp_path: Path) -> None:
    fs = FakeFileSystem()
    populate_system(fs)
    runtime = make_runtime(tmp_path, fs=fs)
    fs.atomic_write(runtime.config_path, "config", mode=0o600)
    fs.atomic_write(runtime.config.data_root / "events" / "x" / "file.jpg", "photo")
    refused = Installer(runtime).uninstall(mode="full-wipe", yes=True, confirm="yes")
    assert not refused.ok
    assert fs.exists(runtime.config.data_root / "events" / "x" / "file.jpg")
    wiped = Installer(runtime).uninstall(
        mode="full-wipe", yes=True, confirm="FULL WIPE"
    )
    assert wiped.ok
    assert not fs.exists(runtime.config.data_root)
    assert not fs.exists(runtime.config_path)


def test_uninstall_requires_yes(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    assert not Installer(runtime).uninstall(mode="services-only", yes=False).ok


def test_backup_restore_roundtrip(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    save_config(config, tmp_path / "etc" / "config.yaml")
    runtime = make_runtime(tmp_path, fs=RealFileSystem(), privileged=True)
    from eventhub.installer.backup import BackupManager

    manager = BackupManager(runtime)
    info = manager.export(include_events=True)
    assert info.path.exists()
    runtime.db.execute(
        "INSERT INTO users(username, password_hash, role, created_at, updated_at)"
        " VALUES ('temp', 'hash', 'viewer', 'now', 'now')"
    )
    config_text = config.hotspot.ssid
    changed = config.model_copy(
        update={"hotspot": config.hotspot.model_copy(update={"ssid": "Changed"})}
    )
    save_config(changed, tmp_path / "etc" / "config.yaml")
    manager.restore(info.path)
    assert runtime.db.scalar("SELECT COUNT(*) FROM users") == 0
    restored = load_config(tmp_path / "etc" / "config.yaml")
    assert restored.hotspot.ssid == config_text
    assert len(manager.list_backups()) == 1
