"""copyparty web file server integration."""

from __future__ import annotations

import contextlib
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

_PERMISSION_ORDER = {
    "rwmda": 5,
    "rwmd": 4,
    "rw": 3,
    "r": 2,
    "w": 1,
}


class CopypartyService(ServiceModule):
    name = "fileserver_copyparty"
    title = "copyparty Web Files"
    description = "Resumable browser uploads, previews and a photo-friendly web UI."
    config_attr = "fileserver_copyparty"
    systemd_units = ("eventhub-copyparty.service",)
    depends_on = ("storage", "events", "users")
    packages = ("ffmpeg",)

    @property
    def copyparty(self) -> Any:
        return self.config.fileserver_copyparty

    # -- data gathering ------------------------------------------------
    def _users(self) -> list[dict[str, str]]:
        rows = self.runtime.db.query_all(
            "SELECT username, role, copyparty_hash, enabled, expires_at FROM users "
            "ORDER BY username"
        )
        users: list[dict[str, str]] = []
        for row in rows:
            if not row["enabled"]:
                continue
            password_hash = str(row["copyparty_hash"] or "")
            if not password_hash:
                self.log.warning("user %s has no copyparty hash; skipping", row["username"])
                continue
            users.append(
                {
                    "username": str(row["username"]),
                    "password_hash": password_hash,
                }
            )
        return users

    def _groups(self) -> list[dict[str, Any]]:
        rows = self.runtime.db.query_all(
            "SELECT role, username FROM users WHERE enabled = 1 ORDER BY username"
        )
        by_role: dict[str, list[str]] = {
            "admin": [],
            "editor": [],
            "shooter": [],
            "viewer": [],
        }
        for row in rows:
            by_role.setdefault(str(row["role"]), []).append(str(row["username"]))
        groups = []
        for role, members in by_role.items():
            if members:
                groups.append({"name": role + "s", "members": members})
        return groups

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

    @staticmethod
    def _copyparty_access(permission: PermissionLevel) -> str:
        return {
            PermissionLevel.ADMIN: "rwmda",
            PermissionLevel.WRITE: "rwmd",
            PermissionLevel.UPLOAD: "w",
            PermissionLevel.READ: "r",
            PermissionLevel.NONE: "",
        }[permission]

    def _volumes(self) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        events = [
            dict(row)
            for row in self.runtime.db.query_all(
                "SELECT * FROM events ORDER BY event_date DESC, id DESC"
            )
        ]
        users = [
            dict(row)
            for row in self.runtime.db.query_all(
                "SELECT id, username, role, enabled FROM users WHERE enabled = 1"
            )
        ]
        volumes: list[dict[str, Any]] = []
        active_volume: dict[str, Any] | None = None
        for event in events:
            accs: dict[str, list[str]] = {}
            for user in users:
                permission = self._permission(user, event)
                access = self._copyparty_access(permission)
                if not access:
                    continue
                accs.setdefault(access, []).append(str(user["username"]))
            volume = {
                "slug": str(event["slug"]),
                "path": str(event["path"]),
                "archived": str(event["status"]) == "archived",
                "accs": accs,
            }
            volumes.append(volume)
            if self.config.events.active_event == event["slug"]:
                active_volume = volume
        return volumes, active_volume

    def _inbox_accs(self) -> dict[str, list[str]]:
        rows = self.runtime.db.query_all(
            "SELECT username, role FROM users WHERE enabled = 1 ORDER BY username"
        )
        accs: dict[str, list[str]] = {"w": []}
        for row in rows:
            role = str(row["role"])
            if role in {"admin", "editor"}:
                accs.setdefault("rwmd", []).append(str(row["username"]))
            elif role == "viewer":
                accs.setdefault("r", []).append(str(row["username"]))
        accs["w"].append("@acct")
        return accs

    def _root_accs(self) -> dict[str, list[str]]:
        rows = self.runtime.db.query_all(
            "SELECT username, role FROM users WHERE enabled = 1 ORDER BY username"
        )
        accs: dict[str, list[str]] = {}
        for row in rows:
            if str(row["role"]) == "admin":
                accs.setdefault("rwmda", []).append(str(row["username"]))
        return accs

    def _extra_volumes(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for name, raw in self.copyparty.extra_volumes.items():
            path = str(raw.get("path", ""))
            if not path or not Path(path).is_absolute():
                self.log.warning("ignoring extra volume %s without an absolute path", name)
                continue
            result[name] = {
                "path": path,
                "accs": raw.get("accs", {}),
                "flags": raw.get("flags", ""),
            }
        return result

    # -- service interface --------------------------------------------
    def validate(self, config: AppConfig) -> list[ValidationIssue]:
        issues = super().validate(config)
        if not config.fileserver_copyparty.enabled:
            return issues
        users = self._users()
        if not users:
            issues.append(
                ValidationIssue(
                    IssueLevel.WARNING,
                    "no users with copyparty password hashes; the web UI will have no accounts",
                    "fileserver_copyparty.enabled",
                )
            )
        if not (config.storage.enabled and config.events.enabled):
            issues.append(
                ValidationIssue(
                    IssueLevel.WARNING,
                    "copyparty volumes depend on the storage and events modules",
                    "fileserver_copyparty.enabled",
                )
            )
        for volume in config.fileserver_copyparty.extra_volumes.values():
            path = str(volume.get("path", ""))
            if path and not Path(path).is_absolute():
                issues.append(
                    ValidationIssue(
                        IssueLevel.ERROR,
                        "extra volume paths must be absolute",
                        "fileserver_copyparty.extra_volumes",
                    )
                )
        return issues

    def render(self, config: AppConfig) -> list[RenderedFile]:
        cfg = config.fileserver_copyparty
        volumes, active = self._volumes()
        content = render_template(
            "modules/copyparty/copyparty.conf.j2",
            port=cfg.port,
            bind=",".join(cfg.bind),
            base_url=cfg.base_url,
            hist_dir=str(cfg.home_dir / "hist"),
            landing_dir=str(cfg.home_dir / "landing"),
            thumbnail_age=cfg.thumbnail_age_days * 86400,
            salt=cfg.password_salt.get_secret_value(),
            thumbnails=cfg.thumbnails,
            extra_args=cfg.extra_args,
            users=self._users(),
            groups=self._groups(),
            root_accs=self._root_accs(),
            inbox_accs=self._inbox_accs(),
            volumes=volumes,
            active_volume=active,
            duplicate_detection=config.storage.duplicate_detection,
            archive_read_only=config.events.archive_is_read_only,
            extra_volumes=self._extra_volumes(),
            inbox_dir=str(config.ingest_root),
            minimum_free_gb=config.storage.minimum_free_gb,
        )
        return [
            RenderedFile(
                cfg.config_path,
                content,
                mode=0o600,
                sensitive=True,
                owner=config.install.service_user,
                group=config.install.service_group,
            )
        ]

    def apply(self) -> ActionResult:
        cfg = self.copyparty
        for path in (
            cfg.home_dir,
            cfg.home_dir / "hist",
            cfg.home_dir / "landing",
            self.config.ingest_root,
        ):
            self.runtime.fs.mkdir(path, mode=0o770)
            with contextlib.suppress(KeyError, OSError):
                self.runtime.fs.chown(
                    path,
                    self.config.install.service_user,
                    self.config.install.service_group,
                )
        binary = self.config.install.venv_path / "bin" / "copyparty"
        if not self.runtime.fs.exists(binary) and self.runtime.runner.which("copyparty") is None:
            return ActionResult.failure(
                "copyparty is not installed in the venv; run: pip install 'eventhub[copyparty]'"
            )
        if not self.runtime.systemd.restart(self.systemd_units[0]):
            return ActionResult.failure("could not start eventhub-copyparty.service")
        return ActionResult.success(f"copyparty listening on port {cfg.port}")

    def health_check(self) -> HealthResult:
        cfg = self.copyparty
        if self.runtime.runner.which("ss") is not None:
            result = self.runtime.runner.run(["ss", "-ltn"], timeout=10)
            if result.ok:
                if f":{cfg.port} " in result.stdout or f":{cfg.port}\n" in result.stdout:
                    return HealthResult(True, f"port {cfg.port} listening")
                return HealthResult(False, f"port {cfg.port} is not listening")
        if self.runtime.systemd.is_active(self.systemd_units[0]):
            return HealthResult(True, "service active (port probe unavailable)")
        return HealthResult(False, "service is not active")

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
            detail=health.message,
            units=status.units,
            checked_at=status.checked_at,
        )

    def volumes_list(self) -> list[dict[str, Any]]:
        volumes, _active = self._volumes()
        return volumes


SERVICE = CopypartyService
