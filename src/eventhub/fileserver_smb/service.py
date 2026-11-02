"""Samba file server: shares per event, per-user access and passdb sync."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from eventhub.core.config import AppConfig
from eventhub.core.templates import render_template
from eventhub.core.types import (
    ROLE_PERMISSIONS,
    ActionResult,
    Health,
    HealthResult,
    IssueLevel,
    PermissionLevel,
    RenderedFile,
    ServiceStatus,
    ValidationIssue,
)
from eventhub.service_api import ServiceModule


class SmbService(ServiceModule):
    name = "fileserver_smb"
    title = "SMB File Server"
    description = "Samba shares per event, tuned for RAW photos and video."
    config_attr = "fileserver_smb"
    systemd_units = ("smbd.service",)
    depends_on = ("storage", "events", "users")
    packages = ("samba",)

    def __init__(self, runtime: Any) -> None:
        super().__init__(runtime)
        self.systemd_units = (self._locate_unit(),)

    def _locate_unit(self) -> str:
        if self.runtime.fs.exists(
            "/usr/lib/systemd/system/smb.service"
        ) and not self.runtime.fs.exists("/usr/lib/systemd/system/smbd.service"):
            return "smb.service"
        return "smbd.service"

    @property
    def smb(self) -> Any:
        return self.config.fileserver_smb

    def _samba_name(self, username: str) -> str:
        prefix = self.config.users.system_user_prefix
        return (prefix + username)[:32]

    def _permission(self, user: dict[str, Any], event: dict[str, Any]) -> PermissionLevel:
        override = self.runtime.db.query_one(
            "SELECT permission FROM access WHERE user_id = ? AND event_id = ?",
            (user["id"], event["id"]),
        )
        if override is not None:
            try:
                base = PermissionLevel(str(override["permission"]))
            except ValueError:
                base = PermissionLevel.NONE
        else:
            base = ROLE_PERMISSIONS.get(str(user["role"]), PermissionLevel.NONE)
        if str(event["status"]) == "archived":
            return (
                PermissionLevel.READ
                if base.rank >= PermissionLevel.READ.rank
                else PermissionLevel.NONE
            )
        return base

    def _shares(self) -> list[dict[str, Any]]:
        users = [
            dict(row)
            for row in self.runtime.db.query_all(
                "SELECT id, username, role FROM users WHERE enabled = 1 ORDER BY username"
            )
        ]
        events = [
            dict(row)
            for row in self.runtime.db.query_all(
                "SELECT * FROM events ORDER BY event_date DESC, id DESC"
            )
        ]
        group = self.config.users.default_group
        shares: list[dict[str, Any]] = []
        for event in events:
            readers: list[str] = []
            writers: list[str] = []
            ingest: list[str] = []
            for user in users:
                permission = self._permission(user, event)
                samba = self._samba_name(str(user["username"]))
                if (
                    permission.rank >= PermissionLevel.READ.rank
                    and permission is not PermissionLevel.UPLOAD
                ):
                    readers.append(samba)
                if permission.rank >= PermissionLevel.WRITE.rank:
                    writers.append(samba)
                if permission.rank >= PermissionLevel.UPLOAD.rank:
                    ingest.append(samba)
            archived = str(event["status"]) == "archived"
            if readers:
                shares.append(
                    {
                        "name": f"Event_{event['slug']}",
                        "path": str(event["path"]),
                        "valid_users": readers,
                        "write_users": [] if archived else writers,
                        "browseable": True,
                        "group": group,
                    }
                )
            if ingest and not archived:
                shares.append(
                    {
                        "name": f"Event_{event['slug']}_Ingest",
                        "path": str(Path(str(event["path"])) / "ingest"),
                        "valid_users": ingest,
                        "write_users": ingest,
                        "browseable": True,
                        "group": group,
                    }
                )
            if self.config.events.active_event == event["slug"] and readers:
                shares.append(
                    {
                        "name": "ActiveEvent",
                        "path": str(event["path"]),
                        "valid_users": readers,
                        "write_users": [] if archived else writers,
                        "browseable": True,
                        "group": group,
                    }
                )
        if users:
            inbox_valid = [
                self._samba_name(str(user["username"]))
                for user in users
                if str(user["role"]) in {"admin", "editor", "shooter"}
            ]
            if inbox_valid:
                shares.append(
                    {
                        "name": "Inbox",
                        "path": str(self.config.ingest_root),
                        "valid_users": inbox_valid,
                        "write_users": inbox_valid,
                        "browseable": True,
                        "group": group,
                    }
                )
        return shares

    def _veto_pattern(self) -> str:
        patterns = [p for p in self.smb.veto_files if p and "/" not in p]
        return "/" + "/".join(patterns) + "/" if patterns else ""

    def validate(self, config: AppConfig) -> list[ValidationIssue]:
        issues = super().validate(config)
        if not config.fileserver_smb.enabled:
            return issues
        users = self.runtime.db.scalar("SELECT COUNT(*) FROM users WHERE enabled = 1")
        if not users:
            issues.append(
                ValidationIssue(
                    IssueLevel.WARNING,
                    "no enabled users; SMB shares will not be accessible",
                    "fileserver_smb.enabled",
                )
            )
        if config.fileserver_smb.guest_ok:
            issues.append(
                ValidationIssue(
                    IssueLevel.WARNING,
                    "SMB guest access is enabled; this is not recommended",
                    "fileserver_smb.guest_ok",
                )
            )
        return issues

    def render(self, config: AppConfig) -> list[RenderedFile]:
        cfg = config.fileserver_smb
        content = render_template(
            "modules/smb/smb.conf.j2",
            workgroup=cfg.workgroup,
            server_string=cfg.server_string,
            min_protocol=cfg.min_protocol,
            max_protocol=cfg.max_protocol,
            socket_options=cfg.socket_options,
            aio_read_size=cfg.aio_read_size,
            aio_write_size=cfg.aio_write_size,
            use_sendfile=cfg.use_sendfile,
            strict_sync=cfg.strict_sync,
            veto_files=self._veto_pattern(),
            hide_dot_files=cfg.hide_dot_files,
            shares=self._shares(),
        )
        return [
            RenderedFile(
                cfg.config_path,
                content,
                mode=0o644,
                owner="root",
                group="root",
            )
        ]

    def apply(self) -> ActionResult:
        cfg = self.smb
        if self.runtime.runner.which("testparm") is not None:
            result = self.runtime.runner.run(["testparm", "-s", str(cfg.config_path)], timeout=30)
            if not result.ok:
                return ActionResult.failure(
                    f"smb.conf is invalid: {result.stderr.strip() or result.stdout.strip()}"
                )
        self.runtime.fs.mkdir(self.config.log_dir / "samba", mode=0o750)
        if not self.runtime.systemd.restart(self.systemd_units[0]):
            return ActionResult.failure(
                f"could not start {self.systemd_units[0]}; is Samba installed?"
            )
        return ActionResult.success("Samba configuration applied")

    def health_check(self) -> HealthResult:
        if self.runtime.runner.which("testparm") is not None:
            result = self.runtime.runner.run(
                ["testparm", "-s", str(self.smb.config_path)], timeout=20
            )
            if not result.ok:
                return HealthResult(False, "smb.conf failed validation")
        if self.runtime.runner.which("ss") is not None:
            result = self.runtime.runner.run(["ss", "-ltn"], timeout=10)
            if result.ok and not re.search(r":445\s", result.stdout):
                return HealthResult(False, "port 445 is not listening")
        if not self.runtime.systemd.is_active(self.systemd_units[0]):
            return HealthResult(False, f"{self.systemd_units[0]} is not active")
        return HealthResult(True, "Samba is running")

    # -- account/passdb sync ------------------------------------------
    def _group(self) -> None:
        group = self.config.users.default_group
        result = self.runtime.runner.run(["getent", "group", group], timeout=10)
        if result.returncode != 0:
            self.runtime.runner.run(["groupadd", "--system", group], timeout=30)

    def ensure_system_user(self, username: str) -> ActionResult:
        self._group()
        samba = self._samba_name(username)
        group = self.config.users.default_group
        result = self.runtime.runner.run(["getent", "passwd", samba], timeout=10)
        if result.returncode != 0:
            created = self.runtime.runner.run(
                [
                    "useradd",
                    "--system",
                    "--no-create-home",
                    "--shell",
                    "/usr/sbin/nologin",
                    "--gid",
                    group,
                    samba,
                ],
                timeout=30,
            )
            if not created.ok:
                return ActionResult.failure(
                    f"could not create system user {samba}: {created.stderr.strip()}"
                )
        else:
            self.runtime.runner.run(["usermod", "-a", "-G", group, samba], timeout=30)
        return ActionResult.success(f"system user {samba} ready")

    def sync_user(self, user: Any, password: str) -> ActionResult:
        username = getattr(user, "username", str(user))
        ensured = self.ensure_system_user(username)
        if not ensured.ok:
            return ensured
        if self.runtime.runner.which("smbpasswd") is None:
            return ActionResult.failure("smbpasswd is not installed")
        samba = self._samba_name(username)
        result = self.runtime.runner.run(
            ["smbpasswd", "-s", "-a", samba],
            timeout=30,
            input_text=f"{password}\n{password}\n",
        )
        if not result.ok:
            return ActionResult.failure(f"smbpasswd failed for {samba}: {result.stderr.strip()}")
        return ActionResult.success(f"SMB password set for {samba}")

    def ensure_users(self, users: list[Any]) -> ActionResult:
        failures: list[str] = []
        for user in users:
            username = getattr(user, "username", str(user))
            result = self.ensure_system_user(username)
            if not result.ok:
                failures.append(result.message)
        if failures:
            return ActionResult.failure("; ".join(failures[:5]))
        return ActionResult.success(f"{len(users)} SMB system user(s) ready")

    def remove_user(self, user: Any) -> ActionResult:
        username = getattr(user, "username", str(user))
        samba = self._samba_name(username)
        if self.runtime.runner.which("smbpasswd") is not None:
            self.runtime.runner.run(["smbpasswd", "-x", samba], timeout=30)
        self.runtime.runner.run(["userdel", samba], timeout=30)
        return ActionResult.success(f"removed SMB user {samba}")

    def shares_list(self) -> list[dict[str, Any]]:
        return self._shares()

    def status(self) -> ServiceStatus:
        status = super().status()
        health = self.health_check()
        value = (
            Health.HEALTHY
            if health.healthy is True
            else Health.UNHEALTHY
            if health.healthy is False
            else Health.UNKNOWN
        )
        return ServiceStatus(
            name=status.name,
            installed=status.installed,
            enabled=status.enabled,
            running=status.running,
            healthy=value,
            detail=f"{len(self._shares())} share(s); {health.message}",
            units=status.units,
            checked_at=status.checked_at,
        )


SERVICE = SmbService
