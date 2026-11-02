"""On-device self-test: every service, port, mount and route."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from eventhub.core.context import Runtime
from eventhub.registry import ServiceRegistry


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    ok: bool | None
    detail: str = ""
    skipped: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "skipped": self.skipped,
        }


@dataclass(slots=True)
class SelfTestReport:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(check.ok is not False for check in self.checks)

    @property
    def failed(self) -> list[CheckResult]:
        return [check for check in self.checks if check.ok is False]

    @property
    def skipped(self) -> list[CheckResult]:
        return [check for check in self.checks if check.skipped]

    def add(self, name: str, ok: bool | None, detail: str = "", skipped: bool = False) -> None:
        self.checks.append(CheckResult(name, ok, detail, skipped))

    def summary(self) -> str:
        return (
            f"{len(self.checks) - len(self.failed) - len(self.skipped)} passed, "
            f"{len(self.failed)} failed, {len(self.skipped)} skipped"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "summary": self.summary(),
            "checks": [check.as_dict() for check in self.checks],
        }

    def print(self, console: Any) -> None:
        from rich.table import Table

        table = Table(title=f"EventHub self-test · {self.summary()}")
        table.add_column("Check")
        table.add_column("Result")
        table.add_column("Detail", overflow="fold")
        for check in self.checks:
            result = "SKIP" if check.skipped else "PASS" if check.ok else "FAIL"
            style = "yellow" if check.skipped else "green" if check.ok else "red"
            table.add_row(check.name, f"[{style}]{result}[/{style}]", check.detail)
        console.print(table)


class SelfTest:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.registry = ServiceRegistry(runtime)
        self.report = SelfTestReport()

    def _ports(self) -> set[int] | None:
        if self.runtime.runner.which("ss") is None:
            return None
        ports: set[int] = set()
        for flags in (["ss", "-ltn"], ["ss", "-lun"]):
            result = self.runtime.runner.run(flags, timeout=10)
            if not result.ok:
                continue
            for match in re.finditer(r":(\d+)\s", result.stdout):
                ports.add(int(match.group(1)))
        return ports

    def _check_config(self) -> None:
        try:
            issues = self.registry.validate_all()
            errors = [issue for issue in issues if issue.level.value == "error"]
            if errors:
                self.report.add(
                    "configuration", False, "; ".join(issue.message for issue in errors)
                )
            else:
                warnings = [issue for issue in issues if issue.level.value == "warning"]
                self.report.add(
                    "configuration",
                    True,
                    f"{len(warnings)} warning(s)" if warnings else "valid",
                )
        except Exception as exc:
            self.report.add("configuration", False, str(exc))

    def _check_services(self) -> None:
        for name, status in self.registry.statuses().items():
            if not status.enabled:
                self.report.add(f"service:{name}", True, "disabled", skipped=True)
                continue
            if not status.installed:
                self.report.add(f"service:{name}", False, "not installed")
                continue
            ok = status.running and status.healthy.value != "unhealthy"
            self.report.add(f"service:{name}", ok, status.detail)

    def _check_ports(self) -> None:
        ports = self._ports()
        if ports is None:
            self.report.add("ports", True, "ss is not installed", skipped=True)
            return
        config = self.runtime.config
        wanted: list[tuple[str, int, bool]] = []
        if config.dashboard.enabled:
            wanted.append(("dashboard", config.dashboard.port, True))
        if config.fileserver_copyparty.enabled:
            wanted.append(("copyparty", config.fileserver_copyparty.port, True))
        if config.fileserver_smb.enabled:
            wanted.append(("smb", 445, True))
        if config.hotspot.enabled:
            wanted.extend([("dns", 53, False), ("dhcp", 67, False)])
        for name, port, required in wanted:
            present = port in ports
            self.report.add(
                f"port:{name}",
                bool(present or not required),
                f"{port} {'listening' if present else 'not listening'}",
                skipped=not present and not required,
            )

    def _check_storage(self) -> None:
        if not self.runtime.config.storage.enabled:
            self.report.add("storage", True, "disabled", skipped=True)
            return
        try:
            from eventhub.storage.service import StorageService

            service = StorageService(self.runtime)
            usage = service.usage()
            if usage.get("mounted"):
                detail = f"{usage.get('used_percent')}% used at {usage.get('mount_point')}"
                ok = usage.get("used_percent", 0) < self.runtime.config.storage.critical_percent
                self.report.add("storage", ok, detail)
            elif self.runtime.config.storage.required:
                self.report.add("storage", False, "required drive is not mounted")
            else:
                self.report.add("storage", True, "no drive attached (optional)", skipped=True)
        except Exception as exc:
            self.report.add("storage", False, str(exc))

    def _check_events(self) -> None:
        if not self.runtime.config.events.enabled:
            self.report.add("events", True, "disabled", skipped=True)
            return
        try:
            from eventhub.events.service import EventsService

            service = EventsService(self.runtime)
            root = service.events_root()
            if not self.runtime.fs.exists(root):
                self.report.add("events", False, f"events root missing: {root}")
                return
            active = service.active_event()
            if self.runtime.config.events.active_event and active is None:
                self.report.add("events", False, "active event is missing")
            elif active is not None and not self.runtime.fs.exists(active.path):
                self.report.add("events", False, f"active event folder missing: {active.path}")
            else:
                self.report.add(
                    "events",
                    True,
                    f"{len(service.list_events())} event(s)"
                    + (f"; active {active.slug}" if active else ""),
                )
        except Exception as exc:
            self.report.add("events", False, str(exc))

    def _check_users(self) -> None:
        try:
            admins = int(
                self.runtime.db.scalar(
                    "SELECT COUNT(*) FROM users WHERE role = 'admin' AND enabled = 1"
                )
                or 0
            )
            self.report.add(
                "users", admins > 0, f"{admins} enabled administrator(s)"
            )
        except Exception as exc:
            self.report.add("users", False, str(exc))

    def _check_helper(self) -> None:
        if not self.runtime.config.helper.enabled:
            self.report.add("helper", True, "disabled", skipped=True)
            return
        path = self.runtime.config.helper.socket_path
        exists = self.runtime.fs.exists(path)
        self.report.add("helper socket", exists, str(path))

    def _check_network(self) -> None:
        if not self.runtime.config.network.enabled:
            self.report.add("network", True, "disabled", skipped=True)
            return
        try:
            from eventhub.network.service import NetworkService

            service = NetworkService(self.runtime)
            health = service.health_check()
            self.report.add(
                "nftables",
                health.healthy,
                health.message,
                skipped=health.healthy is None,
            )
            uplink = service.uplink_state()
            self.report.add(
                "uplink",
                True,
                f"{uplink.kind} {uplink.interface} online={uplink.online}",
                skipped=not uplink.online,
            )
        except Exception as exc:
            self.report.add("network", False, str(exc))

    def _check_discovery(self) -> None:
        if not self.runtime.config.discovery.enabled:
            self.report.add("discovery", True, "disabled", skipped=True)
            return
        try:
            from eventhub.discovery.service import DiscoveryService

            health = DiscoveryService(self.runtime).health_check()
            self.report.add("discovery", health.healthy, health.message)
        except Exception as exc:
            self.report.add("discovery", False, str(exc))

    def _check_airdrop(self) -> None:
        if not self.runtime.config.airdrop.enabled:
            self.report.add("airdrop", True, "disabled", skipped=True)
            return
        try:
            from eventhub.airdrop.service import AirdropService

            health = AirdropService(self.runtime).health_check()
            self.report.add("airdrop", health.healthy, health.message)
        except Exception as exc:
            self.report.add("airdrop", False, str(exc))

    def _check_system(self) -> None:
        if not self.runtime.config.system.enabled:
            self.report.add("system", True, "disabled", skipped=True)
            return
        try:
            from eventhub.system.service import SystemService

            health = SystemService(self.runtime).health_check()
            self.report.add("system", health.healthy, health.message)
        except Exception as exc:
            self.report.add("system", False, str(exc))

    def run(self) -> SelfTestReport:
        self.report = SelfTestReport()
        self._check_config()
        self._check_services()
        self._check_helper()
        self._check_users()
        self._check_storage()
        self._check_events()
        self._check_network()
        self._check_ports()
        self._check_discovery()
        self._check_airdrop()
        self._check_system()
        return self.report

