from __future__ import annotations

from pathlib import Path

import pytest

from eventhub.core.config import DriveConfig, StorageConfig
from eventhub.core.errors import ConflictError
from eventhub.core.types import CommandResult, PermissionLevel
from eventhub.events.service import EventsService, slugify
from eventhub.storage.service import StorageService, systemd_escape_path
from tests.support import FakeFileSystem, FakeRunner, FakeSystemd, make_config, make_runtime

LSBLK = (
    '{"blockdevices":[{"name":"sda","rm":true,"model":"Portable SSD",'
    '"children":[{"name":"sda1","uuid":"1234-ABCD","label":"EVENTS",'
    '"fstype":"exfat","size":500000000000,"mountpoint":null,"rm":true}]}]}'
)


def storage_config(tmp_path: Path, **drive: object) -> StorageConfig:
    values = {
        "enabled": True,
        "mount_root": tmp_path / "media",
        "drive": DriveConfig(
            uuid="1234-ABCD",
            fstype="exfat",
            mount_point=tmp_path / "media" / "data",
            **drive,
        ),
    }
    return StorageConfig.model_validate(values)


def storage_runtime(tmp_path: Path, *, mounts: str = "", **drive: object):
    config = make_config(tmp_path, storage=storage_config(tmp_path, **drive))
    fs = FakeFileSystem()
    fs.write_text("/proc/self/mounts", mounts)
    runner = FakeRunner(
        binaries={"systemctl", "lsblk", "umount", "sync", "wipefs", "mkfs.ext4", "mkfs.exfat"},
        responses={
            "lsblk": CommandResult(("lsblk",), 0, stdout=LSBLK),
        },
    )
    systemd = FakeSystemd()
    return make_runtime(tmp_path, config=config, fs=fs, runner=runner, systemd=systemd)


def test_systemd_escape_path() -> None:
    assert systemd_escape_path("/media/eventhub/data") == "media-eventhub-data.mount"
    assert systemd_escape_path("/media/my-disk") == "media-my\\x2ddisk.mount"


def test_storage_render_mount_unit(tmp_path: Path) -> None:
    runtime = storage_runtime(tmp_path)
    files = StorageService(runtime).render(runtime.config)
    assert len(files) == 1
    assert files[0].path.name == "media-data.mount" or "media" in files[0].path.name
    content = files[0].content
    assert "What=/dev/disk/by-uuid/1234-ABCD" in content
    assert f"Where={tmp_path / 'media' / 'data'}" in content
    assert "Type=exfat" in content
    assert "nofail" not in content
    assert "Options=noatime" in content


def test_storage_apply_enables_and_starts_unit(tmp_path: Path) -> None:
    runtime = storage_runtime(tmp_path)
    result = StorageService(runtime).apply()
    assert result.ok, result.message
    unit = systemd_escape_path(tmp_path / "media" / "data")
    assert ("enable", unit) in runtime.systemd.actions
    assert ("start", unit) in runtime.systemd.actions


def test_storage_unmount_calls_systemd_and_umount(tmp_path: Path) -> None:
    mount = tmp_path / "media" / "data"
    runtime = storage_runtime(tmp_path, mounts=f"/dev/sda1 {mount} exfat rw 0 0\n")
    result = StorageService(runtime).unmount()
    assert result.ok, result.message
    assert runtime.runner.called("umount")
    assert ("stop", systemd_escape_path(mount)) in runtime.systemd.actions


def test_storage_usage_uses_statvfs(tmp_path: Path) -> None:
    mount = tmp_path / "media" / "data"
    runtime = storage_runtime(tmp_path, mounts=f"/dev/sda1 {mount} exfat rw 0 0\n")
    runtime.fs.free_bytes = 400 * 1024**3
    runtime.fs.total_bytes = 500 * 1024**3
    usage = StorageService(runtime).usage()
    assert usage["mounted"]
    assert usage["used_percent"] == 20.0
    assert not usage["critical"]


def test_storage_health_warns_when_nearly_full(tmp_path: Path) -> None:
    mount = tmp_path / "media" / "data"
    runtime = storage_runtime(tmp_path, mounts=f"/dev/sda1 {mount} exfat rw 0 0\n")
    runtime.fs.free_bytes = 5 * 1024**3
    runtime.fs.total_bytes = 500 * 1024**3
    health = StorageService(runtime).health_check()
    assert health.healthy is False
    assert "critically" in health.message


def test_storage_health_without_drive_is_not_fatal(tmp_path: Path) -> None:
    config = make_config(tmp_path, storage=StorageConfig(enabled=True, required=False))
    runtime = make_runtime(tmp_path, config=config)
    health = StorageService(runtime).health_check()
    assert health.healthy is True


def test_storage_format_requires_token(tmp_path: Path) -> None:
    runtime = storage_runtime(tmp_path)
    result = StorageService(runtime).format(fstype="ext4", confirmation="yes")
    assert not result.ok
    assert not runtime.runner.called("mkfs")
    result = StorageService(runtime).format(fstype="ext4", confirmation="FORMAT")
    assert result.ok, result.message
    assert runtime.runner.called("mkfs.ext4")
    assert runtime.runner.called("wipefs -a /dev/sda1")


def test_storage_format_rejects_non_removable(tmp_path: Path) -> None:
    runtime = storage_runtime(tmp_path)
    runtime.runner.responses["lsblk"] = CommandResult(
        ("lsblk",),
        0,
        stdout=(
            '{"blockdevices":[{"name":"sda","rm":false,"children":[{"name":"sda1",'
            '"uuid":"1234-ABCD","fstype":"exfat","size":1000,"rm":false}]}]}'
        ),
    )
    result = StorageService(runtime).format(fstype="ext4", confirmation="FORMAT")
    assert not result.ok
    assert "removable" in result.message


def test_slugify() -> None:
    assert slugify("Emma & Tom's Wedding") == "emma-tom-s-wedding"
    assert slugify("   ") == "event"
    assert len(slugify("x" * 200)) == 48


def event_runtime(tmp_path: Path):
    return make_runtime(tmp_path)


def test_create_event_layout_and_activation(tmp_path: Path) -> None:
    runtime = event_runtime(tmp_path)
    service = EventsService(runtime)
    event = service.create_event(
        "Emma & Tom Wedding", event_date="2026-05-01", activate=True
    )
    assert event.slug == "emma-tom-wedding"
    assert event.status == "active"
    assert runtime.config.events.active_event == event.slug
    for relative in ("ingest/anonymous", "raw", "edit", "exports", "_eventhub"):
        assert runtime.fs.is_dir(event.path / relative)
    assert runtime.fs.exists(event.path / "_eventhub" / "metadata.json")
    assert service.active_event().slug == event.slug
    assert service.get_event(event.slug).status == "active"


def test_event_slug_collision_is_suffixed(tmp_path: Path) -> None:
    runtime = event_runtime(tmp_path)
    service = EventsService(runtime)
    first = service.create_event("Wedding", event_date="2026-05-01")
    second = service.create_event("Wedding", event_date="2026-05-02")
    assert first.slug == "wedding"
    assert second.slug == "wedding-2"


def test_activate_switches_status(tmp_path: Path) -> None:
    runtime = event_runtime(tmp_path)
    service = EventsService(runtime)
    first = service.create_event("One", event_date="2026-01-01", activate=True)
    second = service.create_event("Two", event_date="2026-01-02", activate=False)
    assert service.get_event(first.slug).status == "active"
    service.activate(second.slug)
    assert service.get_event(first.slug).status == "open"
    assert service.get_event(second.slug).status == "active"
    assert runtime.config.events.active_event == second.slug


def test_archive_clears_active_and_blocks_ingest(tmp_path: Path) -> None:
    runtime = event_runtime(tmp_path)
    service = EventsService(runtime)
    event = service.create_event("Party", event_date="2026-02-02", activate=True)
    archived = service.archive(event.slug)
    assert archived.archived
    assert runtime.config.events.active_event is None
    assert runtime.fs.exists(event.path / "_eventhub" / "ARCHIVED")
    with pytest.raises(ConflictError):
        service.ingest_target()
    restored = service.restore(event.slug)
    assert not restored.archived


def test_ingest_target_creates_user_folder(tmp_path: Path) -> None:
    runtime = event_runtime(tmp_path)
    service = EventsService(runtime)
    event = service.create_event("Shoot", event_date="2026-03-03", activate=True)
    target = service.ingest_target("alice")
    assert target == event.path / "ingest" / "alice"
    assert runtime.fs.is_dir(target)
    bad = service.ingest_target("../../etc")
    assert bad.name == "anonymous"


def test_event_apply_keeps_layout(tmp_path: Path) -> None:
    runtime = event_runtime(tmp_path)
    service = EventsService(runtime)
    event = service.create_event("Shoot", event_date="2026-03-03", activate=True)
    runtime.fs.rmtree(event.path / "raw")
    result = service.apply()
    assert result.ok
    assert runtime.fs.is_dir(event.path / "raw")


def test_event_health_and_size(tmp_path: Path) -> None:
    runtime = event_runtime(tmp_path)
    service = EventsService(runtime)
    event = service.create_event("Shoot", event_date="2026-03-03", activate=True)
    runtime.fs.atomic_write(event.path / "raw" / "a.jpg", "x" * 1000)
    assert service.size_bytes(event.slug) >= 1000
    health = service.health_check()
    assert health.healthy is True
    assert "active" in health.message


def test_event_permissions_role_and_override(tmp_path: Path) -> None:
    runtime = event_runtime(tmp_path)
    service = EventsService(runtime)
    event = service.create_event("Shoot", event_date="2026-03-03")
    now = "2026-01-01T00:00:00+00:00"
    user_id = runtime.db.execute(
        "INSERT INTO users(username, password_hash, role, created_at, updated_at)"
        " VALUES ('alice', 'hash', 'editor', ?, ?)",
        (now, now),
    )
    assert service.permission_for(user_id, event.id) is PermissionLevel.WRITE
    service.set_access(user_id, event.id, PermissionLevel.READ)
    assert service.permission_for(user_id, event.id) is PermissionLevel.READ
    service.set_access(user_id, event.id, PermissionLevel.NONE)
    assert service.permission_for(user_id, event.id) is PermissionLevel.WRITE


def test_events_blocked_when_storage_missing(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        storage=storage_config(tmp_path),
    )
    runtime = make_runtime(tmp_path, config=config, fs=FakeFileSystem())
    service = EventsService(runtime)
    with pytest.raises(ConflictError):
        service.create_event("Blocked", event_date="2026-01-01")


def test_events_allowed_once_storage_mounted(tmp_path: Path) -> None:
    mount = tmp_path / "media" / "data"
    config = make_config(tmp_path, storage=storage_config(tmp_path))
    fs = FakeFileSystem()
    fs.write_text("/proc/self/mounts", f"/dev/sda1 {mount} exfat rw 0 0\n")
    runtime = make_runtime(tmp_path, config=config, fs=fs)
    service = EventsService(runtime)
    event = service.create_event("Mounted", event_date="2026-01-01")
    assert str(event.path).startswith(str(mount))
    assert fs.is_dir(event.path)



def test_ingest_refuses_when_free_space_below_minimum(tmp_path: Path) -> None:
    runtime = event_runtime(tmp_path)
    service = EventsService(runtime)
    service.create_event("Shoot", event_date="2026-03-03", activate=True)
    runtime.fs.total_bytes = 10 * 1024**3
    runtime.fs.free_bytes = 1 * 1024**3
    runtime.config.storage.minimum_free_gb = 2
    with pytest.raises(ConflictError):
        service.ingest_target("alice")
