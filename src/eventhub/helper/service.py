"""Privileged helper service module (status/control of the root daemon)."""

from __future__ import annotations

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
from eventhub.service_api import ServiceModule


class HelperService(ServiceModule):
    name = "helper"
    title = "Privileged Helper"
    description = "Narrow root socket used by the unprivileged dashboard."
    config_attr = "helper"
    systemd_units = ("eventhub-helper.service",)
    packages = ()

    @property
    def helper(self) -> object:
        return self.config.helper

    def validate(self, config: AppConfig) -> list[ValidationIssue]:
        issues = super().validate(config)
        if config.helper.enabled and not config.helper.socket_path.is_absolute():
            issues.append(
                ValidationIssue(
                    IssueLevel.ERROR,
                    "helper.socket_path must be absolute",
                    "helper.socket_path",
                )
            )
        return issues

    def render(self, config: AppConfig) -> list[RenderedFile]:
        return []

    def apply(self) -> ActionResult:
        # Restarting the helper from inside the helper would kill the apply.
        # The unit is restarted only by systemctl or the installer.
        return ActionResult.success("helper configuration is applied by the unit")

    def start(self) -> ActionResult:
        result = super().start()
        if result.ok:
            return ActionResult.success("helper started")
        return result

    def health_check(self) -> HealthResult:
        if not self.config.helper.enabled:
            return HealthResult(True, "helper disabled")
        if not self.runtime.fs.exists(self.config.helper.socket_path):
            return HealthResult(False, "helper socket is missing")
        return HealthResult(True, "helper socket ready")

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


SERVICE = HelperService
