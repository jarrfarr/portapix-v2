from __future__ import annotations

from pathlib import Path

import pytest

from eventhub.core.errors import ConflictError
from eventhub.core.types import CommandResult, PermissionLevel
from eventhub.fileserver_copyparty.service import CopypartyService
from eventhub.fileserver_smb.service import SmbService
from eventhub.users.service import UsersService
from tests.support import FakeRunner, FakeSystemd, make_config, make_runtime


def users_runtime(tmp_path: Path, *, binaries: set[str] | None = None):
    config = make_config(tmp_path)
    runner = FakeRunner(
        binaries=binaries or {"systemctl", "getent", "useradd", "usermod", "groupadd", "userdel"}
    )
    return make_runtime(tmp_path, config=config, runner=runner)


def add_user(service: UsersService, name: str = "alice", role: str = "editor", **kwargs: object):
    return service.create_user(
        name,
        "a-strong-password",
        role=role,
        must_change_password=False,
        **kwargs,
    )


def test_create_user_hashes_and_copyparty_hash(tmp_path: Path) -> None:
    runtime = users_runtime(tmp_path)
    service = UsersService(runtime)
    user = add_user(service)
    assert user.username == "alice"
    assert user.role == "editor"
    row = runtime.db.query_one("SELECT * FROM users WHERE id = ?", (user.id,))
    assert row is not None
    assert row["password_hash"].startswith("$argon2")
    assert row["copyparty_hash"].startswith("+")
    assert row["password_hash"] != "a-strong-password"


def test_duplicate_user_rejected(tmp_path: Path) -> None:
    service = UsersService(users_runtime(tmp_path))
    add_user(service)
    with pytest.raises(ConflictError):
        add_user(service)


def test_invalid_username_and_short_password(tmp_path: Path) -> None:
    service = UsersService(users_runtime(tmp_path))
    with pytest.raises(ConflictError):
        service.create_user("Bad Name", "a-strong-password", role="editor")
    with pytest.raises(ConflictError):
        service.create_user("bob", "short", role="editor")


def test_authenticate_and_last_login(tmp_path: Path) -> None:
    runtime = users_runtime(tmp_path)
    service = UsersService(runtime)
    add_user(service)
    assert service.authenticate("alice", "wrong-password") is None
    user = service.authenticate("alice", "a-strong-password")
    assert user is not None
    assert user.last_login_at is not None
    service.update_user(user.id, enabled=False)
    assert service.authenticate("alice", "a-strong-password") is None


def test_password_change_and_sync(tmp_path: Path) -> None:
    runtime = users_runtime(
        tmp_path,
        binaries={"systemctl", "getent", "useradd", "usermod", "groupadd", "smbpasswd"},
    )
    service = UsersService(runtime)
    user = add_user(service)
    result = service.change_password(user.id, "a-strong-password", "another-strong-password")
    assert result.ok, result.message
    assert service.authenticate("alice", "another-strong-password") is not None
    assert service.authenticate("alice", "a-strong-password") is None
    assert runtime.runner.called(r"smbpasswd -s -a eh_alice")


def test_last_admin_cannot_be_deleted_or_demoted(tmp_path: Path) -> None:
    runtime = users_runtime(tmp_path)
    service = UsersService(runtime)
    admin = service.create_user(
        "admin", "admin-password-123", role="admin", must_change_password=False
    )
    with pytest.raises(ConflictError):
        service.delete_user(admin.id)
    with pytest.raises(ConflictError):
        service.update_user(admin.id, role="editor")
    with pytest.raises(ConflictError):
        service.update_user(admin.id, enabled=False)
    second = service.create_user(
        "admin2", "admin-password-456", role="admin", must_change_password=False
    )
    assert service.delete_user(second.id).ok
    assert service.get_user(admin.id).role == "admin"


def test_guest_account_expires(tmp_path: Path) -> None:
    runtime = users_runtime(tmp_path)
    service = UsersService(runtime)
    user, password = service.create_guest_account(hours=1, role="shooter")
    assert user.is_guest
    assert user.expires_at is not None
    assert service.authenticate(user.username, password) is not None
    runtime.db.execute(
        "UPDATE users SET expires_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", user.id),
    )
    assert service.authenticate(user.username, password) is None


def test_guest_link_one_time_and_expiry(tmp_path: Path) -> None:
    service = UsersService(users_runtime(tmp_path))
    link = service.create_guest_link(permission="upload", hours=1, max_uses=2)
    assert service.verify_guest_link(link.token) is not None
    assert service.verify_guest_link(link.token) is not None
    assert service.verify_guest_link(link.token) is None
    assert service.verify_guest_link("bogus") is None


def test_groups(tmp_path: Path) -> None:
    runtime = users_runtime(tmp_path)
    service = UsersService(runtime)
    user = add_user(service)
    group = service.create_group("photographers", "Event photographers")
    assert service.add_to_group(user.id, group.id).ok
    groups = service.list_groups()
    assert groups[0].members == ("alice",)
    assert service.remove_from_group(user.id, group.id).ok
    assert service.list_groups()[0].members == ()
    assert service.delete_group(group.id).ok


def test_users_health_and_apply(tmp_path: Path) -> None:
    runtime = users_runtime(tmp_path)
    service = UsersService(runtime)
    service.create_user("admin", "admin-password-123", role="admin", must_change_password=False)
    assert service.health_check().healthy is True
    result = service.apply()
    assert result.ok


def test_expired_guests_are_purged(tmp_path: Path) -> None:
    runtime = users_runtime(tmp_path)
    service = UsersService(runtime)
    user, _password = service.create_guest_account(hours=1)
    runtime.db.execute(
        "UPDATE users SET expires_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", user.id),
    )
    assert service.purge_expired_guests() == 1
    assert service.get_by_username(user.username) is None


# ---------------------------------------------------------------------------
# file servers
# ---------------------------------------------------------------------------


def populated_runtime(tmp_path: Path, *, archived: bool = False):
    runtime = users_runtime(
        tmp_path,
        binaries={
            "systemctl",
            "getent",
            "useradd",
            "usermod",
            "groupadd",
            "smbpasswd",
            "ss",
            "testparm",
            "copyparty",
        },
    )
    users = UsersService(runtime)
    admin = users.create_user(
        "admin", "admin-password-123", role="admin", must_change_password=False
    )
    editor = users.create_user(
        "editor", "editor-password-123", role="editor", must_change_password=False
    )
    shooter = users.create_user(
        "shooter", "shooter-password-123", role="shooter", must_change_password=False
    )
    viewer = users.create_user(
        "viewer", "viewer-password-123", role="viewer", must_change_password=False
    )
    from eventhub.events.service import EventsService

    events = EventsService(runtime)
    event = events.create_event("Wedding", event_date="2026-05-01", activate=True)
    if archived:
        events.archive(event.slug)
        event = events.get_event(event.slug)
    return runtime, (admin, editor, shooter, viewer), event


def test_copyparty_render_accounts_and_volumes(tmp_path: Path) -> None:
    runtime, _users, event = populated_runtime(tmp_path)
    service = CopypartyService(runtime)
    files = service.render(runtime.config)
    assert len(files) == 1
    assert files[0].mode == 0o600
    config = files[0].content
    assert "[accounts]" in config
    assert "admin: +" in config
    assert "[groups]" in config
    assert "[/inbox]" in config
    assert f"[/events/{event.slug}]" in config
    assert "[/active]" in config
    assert "rwmd" in config
    assert "w: shooter" in config
    assert "ah-salt:" in config


def test_copyparty_archived_volume_is_read_only(tmp_path: Path) -> None:
    runtime, _users, event = populated_runtime(tmp_path, archived=True)
    config = CopypartyService(runtime).render(runtime.config)[0].content
    section = config.split(f"[/events/{event.slug}]", 1)[1].split("[/", 1)[0]
    assert "rwmd" not in section
    assert "r: admin" in section or "r: admin, editor" in section


def test_copyparty_access_override(tmp_path: Path) -> None:
    runtime, users, event = populated_runtime(tmp_path)
    _admin, _editor, _shooter, viewer = users
    from eventhub.events.service import EventsService

    EventsService(runtime).set_access(viewer.id, event.id, PermissionLevel.WRITE)
    config = CopypartyService(runtime).render(runtime.config)[0].content
    assert "rwmd: editor, viewer" in config or "rwmd: viewer, editor" in config


def test_copyparty_apply_and_health(tmp_path: Path) -> None:
    runtime, _users, _event = populated_runtime(tmp_path)
    runtime.fs.atomic_write(runtime.config.install.venv_path / "bin" / "copyparty", "#!/bin/sh\n")
    systemd = FakeSystemd(active={"eventhub-copyparty.service"})
    runtime.systemd = systemd
    runtime.runner.responses["ss"] = CommandResult(
        ("ss",), 0, stdout="LISTEN 0 128 0.0.0.0:3923 0.0.0.0:*\n"
    )
    service = CopypartyService(runtime)
    result = service.apply()
    assert result.ok, result.message
    assert ("restart", "eventhub-copyparty.service") in systemd.actions
    assert service.health_check().healthy is True


def test_copyparty_validate_without_users(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    issues = CopypartyService(runtime).validate(runtime.config)
    assert any("no users" in issue.message for issue in issues)


def test_smb_render_shares(tmp_path: Path) -> None:
    runtime, _users, event = populated_runtime(tmp_path)
    service = SmbService(runtime)
    config = service.render(runtime.config)[0].content
    assert f"[Event_{event.slug}]" in config
    assert f"[Event_{event.slug}_Ingest]" in config
    assert "[ActiveEvent]" in config
    assert "[Inbox]" in config
    assert "eh_admin" in config
    assert "eh_editor" in config
    event_section = config.split(f"[Event_{event.slug}]", 1)[1].split("[", 1)[0]
    assert "eh_shooter" not in event_section
    ingest_section = config.split(f"[Event_{event.slug}_Ingest]", 1)[1].split("[", 1)[0]
    assert "eh_shooter" in ingest_section
    assert "write list" in ingest_section
    assert "veto files = /.DS_Store/._*/Thumbs.db/desktop.ini/.Spotlight-V100/" in config


def test_smb_archived_has_no_writers(tmp_path: Path) -> None:
    runtime, _users, event = populated_runtime(tmp_path, archived=True)
    config = SmbService(runtime).render(runtime.config)[0].content
    section = config.split(f"[Event_{event.slug}]", 1)[1].split("[", 1)[0]
    assert "write list" not in section
    assert f"[Event_{event.slug}_Ingest]" not in config


def test_smb_apply_and_health(tmp_path: Path) -> None:
    runtime, _users, _event = populated_runtime(tmp_path)
    systemd = FakeSystemd(active={"smbd.service"})
    runtime.systemd = systemd
    runtime.runner.responses["ss"] = CommandResult(
        ("ss",), 0, stdout="LISTEN 0 128 0.0.0.0:445 0.0.0.0:*\n"
    )
    service = SmbService(runtime)
    result = service.apply()
    assert result.ok, result.message
    assert runtime.runner.called(r"testparm -s")
    assert ("restart", "smbd.service") in systemd.actions
    assert service.health_check().healthy is True


def test_smb_user_sync_and_remove(tmp_path: Path) -> None:
    runtime, users, _event = populated_runtime(tmp_path)
    service = SmbService(runtime)
    assert service.sync_user(users[0], "admin-password-123").ok
    assert runtime.runner.called(r"smbpasswd -s -a eh_admin")
    assert service.remove_user(users[0]).ok
    assert runtime.runner.called(r"smbpasswd -x eh_admin")
    assert runtime.runner.called(r"userdel eh_admin")
