"""System metrics, journald tuning, timezone, power and updates."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from eventhub.core.config import AppConfig
from eventhub.core.context import Runtime
from eventhub.core.metrics import collect_metrics
from eventhub.core.templates import render_template
from eventhub.core.types import (
    ActionResult,
    Health,
    HealthResult,
    IssueLevel,
    RenderedFile,
    ServiceStatus,
    ValidationIssue,
)
from eventhub.service_api import ServiceModule


def metrics_path(runtime: Runtime) -> Path:
    return Path("/run/eventhub/metrics.json")


class SystemService(ServiceModule):
    name = "system"
    title = "System"
    description = "Metrics, power monitoring, journald, timezone, reboot and update."
    config_attr = "system"
    systemd_units = ("eventhub-metrics.service",)
    packages = ("util-linux", "smartmontools")

    @property
    def system(self) -> Any:
        return self.config.system

    def validate(self, config: AppConfig) -> list[ValidationIssue]:
        issues = super().validate(config)
        if config.system.enabled:
            try:
                ZoneInfo(config.timezone)
            except (ZoneInfoNotFoundError, ValueError):
                issues.append(
                    ValidationIssue(
                        IssueLevel.ERROR,
                        f"unknown timezone: {config.timezone}",
                        "timezone",
                    )
                )
            if config.system.overlay_root:
                issues.append(
                    ValidationIssue(
                        IssueLevel.WARNING,
                        "overlay_root is a documented manual toggle; this build does not "
                        "flip the root filesystem automatically",
                        "system.overlay_root",
                    )
                )
        return issues

    def render(self, config: AppConfig) -> list[RenderedFile]:
        if not config.system.enabled:
            return []
        content = render_template(
            "modules/system/journald.conf.j2",
            max_use=config.system.journald_max_use,
            keep_free=config.system.journald_keep_free,
            max_file_size=config.system.journald_max_file_size,
            retention_days=config.system.log_retention_days,
        )
        return [
            RenderedFile(
                Path("/etc/systemd/journald.conf.d/90-eventhub.conf"),
                content,
                mode=0o644,
                owner="root",
                group="root",
            )
        ]

    def apply(self) -> ActionResult:
        if not self.system.enabled:
            return ActionResult.success("system module disabled")
        if self.runtime.runner.which("timedatectl") is not None:
            result = self.runtime.runner.run(
                ["timedatectl", "set-timezone", self.config.timezone], timeout=20
            )
            if not result.ok:
                self.log.warning("could not set timezone: %s", result.stderr.strip())
        self.runtime.systemd.daemon_reload()
        self.runtime.systemd.restart("systemd-journald.service")
        if not self.runtime.systemd.restart(self.systemd_units[0]):
            return ActionResult.failure("could not start eventhub-metrics.service")
        return ActionResult.success("system settings applied")

    def health_check(self) -> HealthResult:
        if not self.system.enabled:
            return HealthResult(True, "system module disabled")
        if not self.runtime.systemd.is_active(self.systemd_units[0]):
            return HealthResult(False, "metrics service is not active")
        path = metrics_path(self.runtime)
        if not self.runtime.fs.exists(path):
            return HealthResult(True, "metrics service active; no sample yet")
        try:
            payload = json.loads(self.runtime.fs.read_text(path))
        except (OSError, ValueError):
            return HealthResult(False, "metrics file is unreadable")
        return HealthResult(
            True,
            "metrics and power monitor active",
            {"sample": payload},
        )

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

    # -- metrics -------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        metrics = collect_metrics(self.runtime)
        metrics["collected_at"] = self.runtime.clock.now().isoformat()
        return metrics

    def write_metrics(self) -> dict[str, Any]:
        metrics = self.snapshot()
        path = metrics_path(self.runtime)
        self.runtime.fs.mkdir(path.parent, mode=0o750)
        self.runtime.fs.atomic_write(
            path, json.dumps(metrics, indent=2) + "\n", mode=0o644
        )
        return metrics

    def metrics_loop(self, iterations: int | None = None) -> None:
        interval = max(1, self.system.metrics_interval)
        last_throttle: bool | None = None
        count = 0
        self.log.info("metrics loop started every %ss", interval)
        while iterations is None or count < iterations:
            metrics = self.write_metrics()
            throttled = metrics.get("throttled")
            flag = bool(throttled and (throttled.get("throttled") or throttled.get("undervoltage")))
            if last_throttle is not None and flag != last_throttle:
                self.runtime.bus.emit(
                    "system.power_changed",
                    {"throttled": flag, "detail": throttled},
                    source="system.metrics",
                )
            last_throttle = flag
            count += 1
            if iterations is not None and count >= iterations:
                break
            self.runtime.clock.sleep(interval)

    # -- actions -------------------------------------------------------
    def set_timezone(self, timezone: str) -> ActionResult:
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return ActionResult.failure(f"unknown timezone: {timezone}")
        result = self.runtime.runner.run(
            ["timedatectl", "set-timezone", timezone], timeout=20
        )
        if not result.ok:
            return ActionResult.failure(result.stderr.strip() or "timedatectl failed")
        return ActionResult.success(f"timezone set to {timezone}")

    def reboot(self, *, confirm: bool = False) -> ActionResult:
        if not confirm:
            return ActionResult.failure("reboot requires confirm=True")
        result = self.runtime.runner.run(["systemctl", "reboot"], timeout=30)
        return ActionResult(
            result.ok, "rebooting" if result.ok else result.stderr.strip()
        )

    def shutdown(self, *, confirm: bool = False) -> ActionResult:
        if not confirm:
            return ActionResult.failure("shutdown requires confirm=True")
        result = self.runtime.runner.run(["systemctl", "poweroff"], timeout=30)
        return ActionResult(
            result.ok, "shutting down" if result.ok else result.stderr.strip()
        )

    def overlay_status(self) -> dict[str, Any]:
        return {
            "configured": self.system.overlay_root,
            "manual": True,
            "hint": (
                "Use raspi-config or your distro's overlayroot tooling, then reboot. "
                "EventHub does not rewrite the boot cmdline automatically."
            ),
        }


SERVICE = SystemService
