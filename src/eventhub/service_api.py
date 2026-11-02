"""The stable interface every service module implements.

The dashboard, TUI, CLI, helper and installer only ever talk to modules
through this interface and the registry.  Modules never import each other's
internals.
"""

from __future__ import annotations

from abc import ABC
from pathlib import Path
from typing import ClassVar, Protocol, runtime_checkable

from eventhub.core.config import AppConfig
from eventhub.core.context import Runtime
from eventhub.core.db import utcnow
from eventhub.core.types import (
    ActionResult,
    Health,
    HealthResult,
    IssueLevel,
    ModuleInfo,
    RenderedFile,
    ServiceStatus,
    ValidationIssue,
)


@runtime_checkable
class ServiceProvider(Protocol):
    """Anything that can enumerate service modules (normally the registry)."""

    def services(self, *, include_disabled: bool = False) -> list[ServiceModule]: ...

    def get(self, name: str) -> ServiceModule: ...


class ServiceModule(ABC):
    """Base class for a unit of appliance functionality.

    Subclasses set the class attributes, implement :meth:`validate` and
    :meth:`render` where needed, and may override any lifecycle method.
    """

    name: ClassVar[str]
    title: ClassVar[str] = ""
    description: ClassVar[str] = ""
    config_section: ClassVar[str] = ""
    systemd_units: ClassVar[tuple[str, ...]] = ()
    packages: ClassVar[tuple[str, ...]] = ()
    depends_on: ClassVar[tuple[str, ...]] = ()
    requires_root: ClassVar[bool] = True
    requires_reboot: ClassVar[bool] = False
    config_attr: ClassVar[str] = ""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.config: AppConfig = runtime.config
        self.log = runtime.logger.getChild(self.name) if runtime.logger.name else runtime.logger

    # -- helpers -------------------------------------------------------
    @property
    def settings(self) -> object:
        return getattr(self.config, self.config_attr or self.name)

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "enabled", False))

    @property
    def config_key(self) -> str:
        return self.config_attr or self.name

    def info(self, *, available: bool = True) -> ModuleInfo:
        return ModuleInfo(
            name=self.name,
            title=self.title or self.name.replace("_", " ").title(),
            description=self.description,
            config_section=self.config_key,
            available=available,
            requires_root=self.requires_root,
            requires_reboot=self.requires_reboot,
            depends_on=self.depends_on,
        )

    # -- interface -----------------------------------------------------
    def validate(self, config: AppConfig) -> list[ValidationIssue]:
        """Return problems with the module's config section.

        Warnings do not block an apply; errors do.
        """

        section = getattr(config, self.config_key, None)
        if section is None:
            return [
                ValidationIssue(
                    level=IssueLevel.ERROR,
                    message=f"missing config section {self.config_key!r}",
                    field=self.config_key,
                )
            ]
        return []

    def render(self, config: AppConfig) -> list[RenderedFile]:
        """Render declarative files (hostapd, dnsmasq, nftables, ...)."""

        return []

    def apply(self) -> ActionResult:
        """Reload services after rendered files have been written.

        The orchestrator writes rendered files atomically and calls this only
        after every file has been committed.
        """

        return ActionResult.success(f"{self.name} applied")

    def start(self) -> ActionResult:
        return self._systemd_action("start")

    def stop(self) -> ActionResult:
        return self._systemd_action("stop")

    def restart(self) -> ActionResult:
        return self._systemd_action("restart")

    def status(self) -> ServiceStatus:
        return self._default_status()

    def health_check(self) -> HealthResult:
        if not self.systemd_units:
            return HealthResult(healthy=None, message="no daemon to check")
        active = [unit for unit in self.systemd_units if self.runtime.systemd.is_active(unit)]
        if len(active) == len(self.systemd_units):
            return HealthResult(healthy=True, message="all units active")
        if not active:
            return HealthResult(healthy=False, message="no units active")
        return HealthResult(
            healthy=False,
            message=f"inactive units: {', '.join(set(self.systemd_units) - set(active))}",
        )

    # -- default implementations --------------------------------------
    def _systemd_action(self, action: str) -> ActionResult:
        if not self.systemd_units:
            return ActionResult.success(f"{self.name} has no systemd units")
        failures: list[str] = []
        for unit in self.systemd_units:
            method = getattr(self.runtime.systemd, action)
            if not method(unit):
                failures.append(unit)
        if failures:
            return ActionResult.failure(
                f"{action} failed for {', '.join(failures)}", units=failures
            )
        return ActionResult.success(f"{action}ed {self.name}", units=list(self.systemd_units))

    def _default_status(self) -> ServiceStatus:
        enabled = self.enabled
        state = self.runtime.db.service_state(self.name)
        if self.systemd_units:
            installed = bool(state["installed"]) if state else any(
                self.runtime.fs.exists(Path("/etc/systemd/system") / unit)
                for unit in self.systemd_units
            )
            unit_enabled = all(
                self.runtime.systemd.is_enabled(unit) for unit in self.systemd_units
            )
            running = all(self.runtime.systemd.is_active(unit) for unit in self.systemd_units)
            health = self.health_check().healthy
            health_value = (
                Health.HEALTHY
                if health is True
                else Health.UNHEALTHY
                if health is False
                else Health.UNKNOWN
            )
            return ServiceStatus(
                name=self.name,
                installed=installed,
                enabled=unit_enabled and enabled,
                running=running,
                healthy=health_value,
                detail="all units active" if running else "stopped",
                units=self.systemd_units,
            )
        return ServiceStatus(
            name=self.name,
            installed=bool(state["installed"]) if state else enabled,
            enabled=enabled,
            running=enabled,
            healthy=Health.UNKNOWN,
            detail="configuration-only module",
            units=(),
        )

    def mark_installed(self) -> None:
        self.runtime.db.record_service_state(
            self.name,
            installed=True,
            enabled=self.enabled,
            meta=f'{{"updated":"{utcnow()}"}}',
        )

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r} enabled={self.enabled}>"


def combine_statuses(name: str, statuses: list[ServiceStatus]) -> ServiceStatus:
    """Combine per-unit statuses into one module status."""

    if not statuses:
        return ServiceStatus(name=name)
    health = (
        Health.HEALTHY
        if all(s.healthy is Health.HEALTHY for s in statuses)
        else Health.UNHEALTHY
        if any(s.healthy is Health.UNHEALTHY for s in statuses)
        else Health.UNKNOWN
    )
    return ServiceStatus(
        name=name,
        installed=all(s.installed for s in statuses),
        enabled=all(s.enabled for s in statuses),
        running=all(s.running for s in statuses),
        healthy=health,
        detail="; ".join(s.detail for s in statuses if s.detail),
        units=tuple(unit for s in statuses for unit in s.units),
    )
