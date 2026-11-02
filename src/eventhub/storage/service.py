"""Storage module: drive detection, UUID mounts, safe eject and formatting."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from eventhub.core.config import AppConfig, DriveConfig
from eventhub.core.mounts import is_mounted as path_is_mounted
from eventhub.core.templates import render_template
from eventhub.core.types import (
    ActionResult,
    DriveInfo,
    Health,
    HealthResult,
    IssueLevel,
    RenderedFile,
    ServiceStatus,
    ValidationIssue,
)
from eventhub.network.hardware import detect_hardware
from eventhub.service_api import ServiceModule


def systemd_escape_path(path: Path | str) -> str:
    """Convert /media/eventhub/data to media-eventhub-data.mount."""

    text = str(Path(path)).strip("/")
    if not text:
        return "-.mount"
    escaped = text.replace("-", "\\x2d").replace("/", "-")
    return f"{escaped}.mount"


def _clean_options(options: str) -> str:
    """Drop fstab-only options that mount(8) would reject in a .mount unit."""

    allowed = []
    for item in options.split(","):
        item = item.strip()
        if not item:
            continue
        if item in {"defaults", "auto", "noauto", "nofail"}:
            continue
        if item.startswith("x-systemd."):
            continue
        allowed.append(item)
    return ",".join(allowed) or "noatime"


class StorageService(ServiceModule):
    name = "storage"
    title = "Storage"
    description = "USB drive detection, UUID mounts, formatting and free space."
    config_attr = "storage"
    packages = ("exfatprogs", "e2fsprogs", "util-linux")

    @property
    def storage(self) -> Any:
        return self.config.storage

    # -- detection -----------------------------------------------------
    def detected_drives(self) -> list[DriveInfo]:
        try:
            drives = list(detect_hardware(self.runtime).drives)
        except Exception:
            return []
        result: list[DriveInfo] = []
        for drive in drives:
            free = 0
            if drive.mounted and drive.mount_point is not None:
                try:
                    free = self.runtime.fs.statvfs(drive.mount_point).available_bytes
                except OSError:
                    free = 0
            result.append(replace(drive, free_bytes=free))
        return result

    def resolve_drive(self) -> DriveInfo | None:
        cfg: DriveConfig = self.storage.drive
        drives = self.detected_drives()
        if cfg.uuid:
            for drive in drives:
                if drive.uuid.lower() == cfg.uuid.lower():
                    return drive
            fstype = cfg.fstype if cfg.fstype != "auto" else "auto"
            return DriveInfo(
                device=Path("/dev/disk/by-uuid") / cfg.uuid,
                uuid=cfg.uuid,
                fstype="auto" if fstype == "auto" else fstype,
                mount_point=cfg.mount_point if self.is_mounted(cfg.mount_point) else None,
                mounted=self.is_mounted(cfg.mount_point),
                detail="configured by UUID (not currently attached)",
            )
        if cfg.label:
            for drive in drives:
                if drive.label.lower() == cfg.label.lower():
                    return drive
        if cfg.device:
            for drive in drives:
                if str(drive.device) == cfg.device:
                    return drive
            return DriveInfo(
                device=Path(cfg.device),
                fstype=cfg.fstype if cfg.fstype != "auto" else "auto",
                mount_point=cfg.mount_point if self.is_mounted(cfg.mount_point) else None,
                mounted=self.is_mounted(cfg.mount_point),
            )
        candidates = [
            drive
            for drive in drives
            if drive.fstype in {"ext4", "exfat", "xfs", "btrfs", "vfat"}
            and drive.device != Path("/")
        ]
        removable = [drive for drive in candidates if drive.removable]
        return (removable or candidates or [None])[0]

    def is_mounted(self, path: Path | str) -> bool:
        return path_is_mounted(self.runtime.fs, path)

    def usage(self) -> dict[str, Any]:
        drive = self.resolve_drive()
        cfg = self.storage.drive
        mounted = self.is_mounted(cfg.mount_point)
        payload: dict[str, Any] = {
            "drive": drive.as_dict() if hasattr(drive, "as_dict") else None,
            "mount_point": str(cfg.mount_point),
            "mounted": mounted,
            "required": self.storage.required,
        }
        if mounted:
            try:
                stat = self.runtime.fs.statvfs(cfg.mount_point)
                payload.update(
                    {
                        "total_bytes": stat.total_bytes,
                        "free_bytes": stat.available_bytes,
                        "used_bytes": stat.used_bytes,
                        "used_percent": stat.used_percent,
                    }
                )
                payload["warning"] = stat.used_percent >= self.storage.warn_percent
                payload["critical"] = stat.used_percent >= self.storage.critical_percent
            except OSError:
                payload["error"] = "could not read filesystem usage"
        return payload

    def mount_unit(self) -> tuple[str, Path]:
        cfg: DriveConfig = self.storage.drive
        name = systemd_escape_path(cfg.mount_point)
        return name, Path("/etc/systemd/system") / name

    # -- service interface --------------------------------------------
    def validate(self, config: AppConfig) -> list[ValidationIssue]:
        issues = super().validate(config)
        cfg = config.storage
        if not cfg.enabled:
            return issues
        if cfg.required and not (cfg.drive.uuid or cfg.drive.device or cfg.drive.label):
            issues.append(
                ValidationIssue(
                    IssueLevel.ERROR,
                    "storage.required is true but no drive UUID, label or device is configured",
                    "storage.drive",
                )
            )
        if cfg.required and not self.is_mounted(cfg.drive.mount_point):
            issues.append(
                ValidationIssue(
                    IssueLevel.WARNING,
                    f"required storage is not mounted at {cfg.drive.mount_point}",
                    "storage.drive",
                )
            )
        if (
            not (cfg.drive.uuid or cfg.drive.device or cfg.drive.label)
            and not self.detected_drives()
        ):
            issues.append(
                ValidationIssue(
                    IssueLevel.WARNING,
                    "no removable drive detected; event data will use the fallback path",
                    "storage.drive",
                )
            )
        return issues

    def render(self, config: AppConfig) -> list[RenderedFile]:
        cfg = config.storage
        if not cfg.enabled or not cfg.drive.auto_mount:
            return []
        drive = self.resolve_drive()
        uuid = cfg.drive.uuid or (drive.uuid if drive else "")
        what = ""
        if uuid:
            what = f"/dev/disk/by-uuid/{uuid}"
        elif cfg.drive.device:
            what = cfg.drive.device
        elif drive is not None and drive.device:
            what = str(drive.device)
        if not what:
            return []
        fstype = cfg.drive.fstype
        if fstype == "auto":
            fstype = drive.fstype if drive and drive.fstype != "auto" else "auto"
        label = cfg.drive.label or (drive.label if drive else "") or uuid or what
        unit = systemd_escape_path(cfg.drive.mount_point)
        content = render_template(
            "modules/storage/mount.unit.j2",
            uuid=uuid,
            what=what,
            where=str(cfg.drive.mount_point),
            fstype=fstype,
            options=_clean_options(cfg.drive.options),
            timeout=10,
            label=label,
        )
        return [
            RenderedFile(
                Path("/etc/systemd/system") / unit,
                content,
                mode=0o644,
                owner="root",
                group="root",
            )
        ]

    def apply(self) -> ActionResult:
        cfg = self.storage
        drive = self.resolve_drive()
        if drive is None or not (cfg.drive.uuid or cfg.drive.device or cfg.drive.label):
            if cfg.required:
                return ActionResult.failure("required storage drive is not attached")
            return ActionResult.success("no storage drive configured; using fallback data path")
        if not cfg.drive.auto_mount:
            return ActionResult.success("automatic mount disabled")
        unit, unit_path = self.mount_unit()
        self.runtime.fs.mkdir(cfg.drive.mount_point, mode=0o750)
        self.runtime.systemd.daemon_reload()
        if not self.runtime.systemd.enable(unit):
            if cfg.required:
                return ActionResult.failure(f"could not enable {unit}")
            self.log.warning("could not enable %s", unit)
        if not self.is_mounted(cfg.drive.mount_point) and not self.runtime.systemd.start(unit):
            if cfg.required:
                return ActionResult.failure(
                    f"could not mount {cfg.drive.mount_point} from {unit_path}"
                )
            return ActionResult.success("storage drive is not attached (optional)")
        self.runtime.bus.emit(
            "storage.mounted",
            {"mount_point": str(cfg.drive.mount_point)},
            source="storage",
        )
        return ActionResult.success(f"storage mounted at {cfg.drive.mount_point}")

    def unmount(self, *, force: bool = False) -> ActionResult:
        cfg = self.storage
        if not self.is_mounted(cfg.drive.mount_point):
            return ActionResult.success("storage is not mounted")
        unit, _ = self.mount_unit()
        self.runtime.systemd.stop(unit)
        self.runtime.runner.run(["sync"], timeout=30)
        result = self.runtime.runner.run(
            ["umount", *(["-f"] if force else []), str(cfg.drive.mount_point)], timeout=60
        )
        if not result.ok:
            return ActionResult.failure(
                f"could not unmount {cfg.drive.mount_point}: {result.stderr.strip()}"
            )
        self.runtime.bus.emit(
            "storage.unmounted",
            {"mount_point": str(cfg.drive.mount_point)},
            source="storage",
        )
        return ActionResult.success(f"ejected {cfg.drive.mount_point}")

    def mount(self) -> ActionResult:
        cfg = self.storage
        unit, _ = self.mount_unit()
        self.runtime.systemd.daemon_reload()
        if not self.runtime.systemd.start(unit):
            return ActionResult.failure(f"could not start {unit}")
        return ActionResult.success(f"storage mounted at {cfg.drive.mount_point}")

    def format(
        self,
        *,
        fstype: str,
        confirmation: str,
        label: str = "EVENTHUB",
    ) -> ActionResult:
        cfg = self.storage
        if not cfg.allow_format:
            return ActionResult.failure("formatting is disabled in config")
        if confirmation != cfg.format_requires_confirmation:
            return ActionResult.failure(
                f"format requires the confirmation token {cfg.format_requires_confirmation!r}"
            )
        if fstype not in {"ext4", "exfat"}:
            return ActionResult.failure("fstype must be ext4 or exfat")
        drive = self.resolve_drive()
        if drive is None or not drive.removable:
            return ActionResult.failure("no removable drive selected")
        device = str(drive.device)
        if not re.fullmatch(r"/dev/[A-Za-z0-9._/-]+", device):
            return ActionResult.failure(f"refusing to format unexpected device path: {device}")
        if self.is_mounted(cfg.drive.mount_point):
            unmounted = self.unmount(force=True)
            if not unmounted.ok:
                return unmounted
        self.runtime.runner.run(["wipefs", "-a", device], timeout=60)
        if fstype == "ext4":
            command = ["mkfs.ext4", "-F", "-L", label, device]
        else:
            command = ["mkfs.exfat", "-n", label, device]
        result = self.runtime.runner.run(command, timeout=600)
        if not result.ok:
            return ActionResult.failure(
                result.stderr.strip() or f"{fstype} format failed"
            )
        self.runtime.bus.emit(
            "storage.formatted", {"device": device, "fstype": fstype}, source="storage"
        )
        return ActionResult.success(f"formatted {device} as {fstype}")

    def health_check(self) -> HealthResult:
        cfg = self.storage
        if not cfg.enabled:
            return HealthResult(True, "storage module disabled")
        mounted = self.is_mounted(cfg.drive.mount_point)
        if mounted:
            usage = self.usage()
            percent = float(usage.get("used_percent", 0))
            if percent >= cfg.critical_percent:
                return HealthResult(
                    False,
                    f"disk critically full ({percent:.0f}%)",
                    usage,
                )
            if percent >= cfg.warn_percent:
                return HealthResult(
                    True,
                    f"disk nearly full ({percent:.0f}%)",
                    usage,
                )
            return HealthResult(True, "storage mounted", usage)
        if cfg.required:
            return HealthResult(False, "required storage is not mounted")
        if cfg.drive.uuid or cfg.drive.device or cfg.drive.label:
            return HealthResult(True, "storage drive not attached", self.usage())
        return HealthResult(True, "no storage drive configured")

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


SERVICE = StorageService
