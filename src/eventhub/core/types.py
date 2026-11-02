"""Typed value objects shared across the core and every module."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

Role = Literal["ap", "airdrop", "wan", "lan"]


class Health(StrEnum):
    """Tri-state health, because "unknown" is different from "broken"."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class IssueLevel(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class PermissionLevel(StrEnum):
    """Permission levels used by events and the access matrix."""

    NONE = "none"
    READ = "read"
    UPLOAD = "upload"
    WRITE = "write"
    ADMIN = "admin"

    @property
    def rank(self) -> int:
        return {
            PermissionLevel.NONE: 0,
            PermissionLevel.READ: 1,
            PermissionLevel.UPLOAD: 2,
            PermissionLevel.WRITE: 3,
            PermissionLevel.ADMIN: 4,
        }[self]

    def at_least(self, other: PermissionLevel) -> bool:
        return self.rank >= other.rank


UserRole = Literal["admin", "editor", "shooter", "viewer"]

ROLE_PERMISSIONS: dict[str, PermissionLevel] = {
    "admin": PermissionLevel.ADMIN,
    "editor": PermissionLevel.WRITE,
    "shooter": PermissionLevel.UPLOAD,
    "viewer": PermissionLevel.READ,
}


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    level: IssueLevel
    message: str
    field: str | None = None

    def as_text(self) -> str:
        prefix = self.level.value
        location = f" [{self.field}]" if self.field else ""
        return f"[{prefix}]{location} {self.message}"


@dataclass(frozen=True, slots=True)
class RenderedFile:
    """A file produced by a module's render step.

    ``owner`` and ``group`` are *names*; the orchestrator resolves them only
    when running as root, and silently skips chown when unprivileged.
    """

    path: Path
    content: str
    mode: int = 0o644
    owner: str | None = None
    group: str | None = None
    sensitive: bool = False


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    name: str
    installed: bool = False
    enabled: bool = False
    running: bool = False
    healthy: Health = Health.UNKNOWN
    detail: str = ""
    units: tuple[str, ...] = ()
    checked_at: float = field(default_factory=time.time)

    @property
    def ok(self) -> bool:
        return (
            self.installed
            and self.enabled
            and self.running
            and self.healthy is not Health.UNHEALTHY
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "installed": self.installed,
            "enabled": self.enabled,
            "running": self.running,
            "healthy": self.healthy.value,
            "detail": self.detail,
            "units": list(self.units),
            "checked_at": self.checked_at,
        }


@dataclass(frozen=True, slots=True)
class HealthResult:
    healthy: bool | None
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ActionResult:
    ok: bool
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, message: str = "", **details: Any) -> ActionResult:
        return cls(ok=True, message=message, details=dict(details))

    @classmethod
    def failure(cls, message: str, **details: Any) -> ActionResult:
        return cls(ok=False, message=message, details=dict(details))


@dataclass(frozen=True, slots=True)
class ApplyResult:
    ok: bool
    message: str
    snapshot_id: str | None = None
    applied: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    issues: tuple[ValidationIssue, ...] = ()
    rolled_back: bool = False
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "snapshot_id": self.snapshot_id,
            "applied": list(self.applied),
            "failed": list(self.failed),
            "rolled_back": self.rolled_back,
            "dry_run": self.dry_run,
            "issues": [
                {"level": i.level.value, "message": i.message, "field": i.field}
                for i in self.issues
            ],
        }


@dataclass(frozen=True, slots=True)
class ModuleInfo:
    """Descriptive metadata used by the dashboard, TUI and CLI."""

    name: str
    title: str
    description: str
    config_section: str
    available: bool = True
    requires_root: bool = True
    requires_reboot: bool = False
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class InterfaceInfo:
    name: str
    mac: str = ""
    driver: str = ""
    usb_id: str = ""
    kind: Literal["wifi", "ethernet", "other"] = "other"
    state: str = "unknown"
    addresses: tuple[str, ...] = ()
    supports_ap: bool = False
    supports_monitor: bool = False
    is_builtin: bool = False
    detail: str = ""


@dataclass(frozen=True, slots=True)
class DriveInfo:
    device: Path
    uuid: str = ""
    label: str = ""
    fstype: str = ""
    size_bytes: int = 0
    free_bytes: int = 0
    mounted: bool = False
    mount_point: Path | None = None
    removable: bool = False
    model: str = ""
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "device": str(self.device),
            "uuid": self.uuid,
            "label": self.label,
            "fstype": self.fstype,
            "size_bytes": self.size_bytes,
            "free_bytes": self.free_bytes,
            "mounted": self.mounted,
            "mount_point": str(self.mount_point) if self.mount_point else None,
            "removable": self.removable,
            "model": self.model,
            "detail": self.detail,
        }
