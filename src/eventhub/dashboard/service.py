"""Dashboard service module: render settings and control uvicorn."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from eventhub.core.config import AppConfig
from eventhub.core.types import (
    ActionResult,
    Health,
    HealthResult,
    IssueLevel,
    RenderedFile,
    ServiceStatus,
    ValidationIssue,
)
from eventhub.dashboard.settings import DashboardSettings
from eventhub.service_api import ServiceModule


class DashboardService(ServiceModule):
    name = "dashboard"
    title = "Dashboard"
    description = "Mobile-friendly web administration UI and API."
    config_attr = "dashboard"
    systemd_units = ("eventhub-dashboard.service",)
    depends_on = ("helper", "users", "events", "storage", "network", "hotspot")
    packages = ()

    @property
    def dashboard(self) -> Any:
        return self.config.dashboard

    def validate(self, config: AppConfig) -> list[ValidationIssue]:
        issues = super().validate(config)
        if not config.dashboard.enabled:
            return issues
        if len(config.dashboard.secret_key.get_secret_value()) < 32:
            issues.append(
                ValidationIssue(
                    IssueLevel.ERROR,
                    "dashboard.secret_key must be at least 32 characters",
                    "dashboard.secret_key",
                )
            )
        if not config.helper.enabled:
            issues.append(
                ValidationIssue(
                    IssueLevel.ERROR,
                    "the dashboard requires the privileged helper",
                    "helper.enabled",
                )
            )
        return issues

    def render(self, config: AppConfig) -> list[RenderedFile]:
        if not config.dashboard.enabled:
            return []
        settings = DashboardSettings.from_config(config)
        return [
            RenderedFile(
                Path("/etc/eventhub/dashboard.json"),
                settings.to_json(),
                mode=0o640,
                owner="root",
                group=config.install.dashboard_group,
                sensitive=True,
            )
        ]

    def apply(self) -> ActionResult:
        if not self.dashboard.enabled:
            return ActionResult.success("dashboard disabled")
        if not self.runtime.systemd.restart(self.systemd_units[0]):
            return ActionResult.failure(
                "could not start eventhub-dashboard.service; check journalctl"
            )
        return ActionResult.success(f"dashboard listening on port {self.dashboard.port}")

    def health_check(self) -> HealthResult:
        if not self.dashboard.enabled:
            return HealthResult(True, "dashboard disabled")
        if not self.runtime.systemd.is_active(self.systemd_units[0]):
            return HealthResult(False, "dashboard service is not active")
        if self.runtime.runner.which("curl") is not None:
            result = self.runtime.runner.run(
                [
                    "curl",
                    "-fsS",
                    "--max-time",
                    "5",
                    "-o",
                    "/dev/null",
                    f"http://127.0.0.1:{self.dashboard.port}/login",
                ],
                timeout=10,
            )
            if not result.ok:
                return HealthResult(False, "dashboard did not answer /login")
        return HealthResult(True, f"http://{self.config.discovery.hostname}.local/")

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


SERVICE = DashboardService
