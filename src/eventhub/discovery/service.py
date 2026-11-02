"""mDNS discovery and Avahi service advertisement."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from eventhub.core.config import AppConfig
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
from eventhub.network.roles import resolve_roles
from eventhub.service_api import ServiceModule

_SERVICE_FILES = {
    "smb": "eventhub-smb.service",
    "copyparty": "eventhub-copyparty.service",
    "dashboard": "eventhub-dashboard.service",
}


class DiscoveryService(ServiceModule):
    name = "discovery"
    title = "mDNS Discovery"
    description = "Avahi hostname and SMB/copyparty/dashboard advertisement."
    config_attr = "discovery"
    systemd_units = ("avahi-daemon.service",)
    depends_on = ("network",)
    packages = ()

    @property
    def discovery(self) -> Any:
        return self.config.discovery

    def interfaces(self, config: AppConfig | None = None) -> list[str]:
        cfg = config or self.config
        if cfg.discovery.interfaces:
            return [name for name in cfg.discovery.interfaces if name not in {"auto", "none"}]
        roles = resolve_roles(cfg)
        return [name for name in (roles.ap, roles.lan) if name]

    def _service(
        self,
        config: AppConfig,
        *,
        name: str,
        service_type: str,
        port: int,
        txt: list[str] | None = None,
    ) -> RenderedFile:
        content = render_template(
            "modules/discovery/service.xml.j2",
            service_name=name,
            service_type=service_type,
            port=port,
            txt_records=txt or [],
        )
        return RenderedFile(
            Path("/etc/avahi/services") / _SERVICE_FILES[name],
            content,
            mode=0o644,
            owner="root",
            group="root",
        )

    def validate(self, config: AppConfig) -> list[ValidationIssue]:
        issues = super().validate(config)
        if not config.discovery.enabled:
            return issues
        if self.runtime.runner.which("avahi-daemon") is None:
            issues.append(
                ValidationIssue(
                    IssueLevel.WARNING,
                    "avahi-daemon is not installed; mDNS names will not resolve",
                    "discovery.enabled",
                )
            )
        if not self.interfaces(config):
            issues.append(
                ValidationIssue(
                    IssueLevel.WARNING,
                    "no AP or LAN interface resolved; Avahi may advertise on every interface",
                    "discovery.interfaces",
                )
            )
        return issues

    def render(self, config: AppConfig) -> list[RenderedFile]:
        cfg = config.discovery
        if not cfg.enabled:
            return []
        files = [
            RenderedFile(
                Path("/etc/avahi/avahi-daemon.conf"),
                render_template(
                    "modules/discovery/avahi-daemon.conf.j2",
                    interfaces=self.interfaces(config),
                ),
                mode=0o644,
                owner="root",
                group="root",
            )
        ]
        if cfg.advertise_smb and config.fileserver_smb.enabled:
            files.append(
                self._service(
                    config,
                    name="smb",
                    service_type="_smb._tcp",
                    port=445,
                )
            )
        if cfg.advertise_copyparty and config.fileserver_copyparty.enabled:
            files.append(
                self._service(
                    config,
                    name="copyparty",
                    service_type="_http._tcp",
                    port=config.fileserver_copyparty.port,
                    txt=["path=/"],
                )
            )
        if cfg.advertise_dashboard and config.dashboard.enabled:
            files.append(
                self._service(
                    config,
                    name="dashboard",
                    service_type="_http._tcp",
                    port=config.dashboard.port,
                    txt=["path=/"],
                )
            )
        return files

    def apply(self) -> ActionResult:
        cfg = self.discovery
        if not cfg.enabled:
            return ActionResult.success("discovery module disabled")
        self.runtime.fs.mkdir("/etc/avahi/services", mode=0o755)
        expected = {
            _SERVICE_FILES["smb"]: cfg.advertise_smb and self.config.fileserver_smb.enabled,
            _SERVICE_FILES["copyparty"]: cfg.advertise_copyparty
            and self.config.fileserver_copyparty.enabled,
            _SERVICE_FILES["dashboard"]: cfg.advertise_dashboard
            and self.config.dashboard.enabled,
        }
        for filename, wanted in expected.items():
            path = Path("/etc/avahi/services") / filename
            if not wanted:
                self.runtime.fs.remove(path, missing_ok=True)
        if self.runtime.runner.which("hostnamectl") is not None:
            self.runtime.runner.run(
                ["hostnamectl", "set-hostname", cfg.hostname], timeout=20
            )
        if not self.runtime.systemd.restart(self.systemd_units[0]):
            return ActionResult.failure("could not restart avahi-daemon")
        return ActionResult.success(
            f"advertising as {cfg.hostname}.{cfg.domain} on "
            + (", ".join(self.interfaces()) or "all interfaces")
        )

    def health_check(self) -> HealthResult:
        if not self.discovery.enabled:
            return HealthResult(True, "discovery module disabled")
        if not self.runtime.systemd.is_active(self.systemd_units[0]):
            return HealthResult(False, "avahi-daemon is not active")
        fqdn = f"{self.discovery.hostname}.{self.discovery.domain}"
        if self.runtime.runner.which("avahi-resolve") is not None:
            result = self.runtime.runner.run(
                ["avahi-resolve", "-n", fqdn], timeout=10
            )
            if not result.ok:
                return HealthResult(False, f"could not resolve {fqdn}")
        return HealthResult(True, f"{fqdn} is advertised", {"fqdn": fqdn})

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


SERVICE = DiscoveryService
