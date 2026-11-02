"""AirDrop receive via omdrop-owl / owly (t4t5/owly).

Owly has two halves: `owlyd` owns the radio, `owly radio up` holds an AWDL
lease, and `owly receive` accepts AirDrop transfers on `awdl0`.  EventHub runs
the radio and receiver as separate systemd units and execs into `owly receive`
so systemd can stop it cleanly.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any

from eventhub.core.config import AppConfig
from eventhub.core.errors import ConfigError
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
from eventhub.network.hardware import detect_hardware
from eventhub.network.roles import resolve_roles
from eventhub.service_api import ServiceModule


class AirdropService(ServiceModule):
    name = "airdrop"
    title = "AirDrop Receive"
    description = "owly (omdrop-owl) receiver for iPhone and Mac transfers."
    config_attr = "airdrop"
    systemd_units = ("eventhub-radio.service", "eventhub-airdrop.service")
    depends_on = ("network", "events")
    packages = ()

    @property
    def airdrop(self) -> Any:
        return self.config.airdrop

    def interface(self, config: AppConfig | None = None) -> str:
        cfg = config or self.config
        if cfg.airdrop.interface not in {"auto", ""}:
            return cfg.airdrop.interface
        try:
            hardware = detect_hardware(self.runtime)
        except Exception:
            hardware = None
        return resolve_roles(cfg, hardware).airdrop

    def target_dir(self) -> Path:
        cfg = self.airdrop
        if cfg.target_follows_active_event:
            try:
                from eventhub.events.service import EventsService

                return EventsService(self.runtime).ingest_target()
            except Exception:
                pass
        if cfg.target_dir is not None:
            return cfg.target_dir
        return self.config.data_root / "inbox"

    def receive_argv(self) -> list[str]:
        cfg = self.airdrop
        return [
            str(cfg.owl_binary),
            "receive",
            "--iface",
            cfg.awdl_interface,
            "--name",
            cfg.device_name,
            "--dir",
            str(self.target_dir()),
            "--accept",
            "--port",
            str(cfg.receive_port),
        ]

    def receive(self) -> None:
        """Resolve the current target and replace this process with owly."""

        cfg = self.airdrop
        target = self.target_dir()
        self.runtime.fs.mkdir(target, mode=0o770)
        with contextlib.suppress(KeyError, OSError):
            self.runtime.fs.chown(
                target,
                self.config.install.airdrop_user,
                self.config.install.service_group,
            )
        if not self.runtime.fs.exists(cfg.owl_binary) and self.runtime.runner.which("owly") is None:
            raise ConfigError(
                f"owly is not installed at {cfg.owl_binary}; run the installer or "
                "build https://github.com/t4t5/owly"
            )
        # Give systemd's radio unit a moment to create awdl0.
        deadline = self.runtime.clock.monotonic() + 30
        while (
            not self.runtime.fs.exists(Path("/sys/class/net") / cfg.awdl_interface)
            and self.runtime.clock.monotonic() < deadline
        ):
            self.runtime.clock.sleep(1)
        argv = self.receive_argv()
        os.execv(argv[0], argv)

    def validate(self, config: AppConfig) -> list[ValidationIssue]:
        issues = super().validate(config)
        if not config.airdrop.enabled:
            return issues
        if (
            not self.runtime.fs.exists(config.airdrop.owl_binary)
            and self.runtime.runner.which("owly") is None
        ):
            issues.append(
                ValidationIssue(
                    IssueLevel.WARNING,
                    "owly is not installed; AirDrop will not start until it is built "
                    "or installed from t4t5/owly",
                    "airdrop.owl_binary",
                )
            )
        interface = self.interface(config)
        if not interface:
            issues.append(
                ValidationIssue(
                    IssueLevel.WARNING,
                    "no AirDrop adapter selected; set roles.airdrop or airdrop.interface",
                    "airdrop.interface",
                )
            )
        if config.airdrop.receive_mode == "contacts":
            issues.append(
                ValidationIssue(
                    IssueLevel.WARNING,
                    "owly only supports Everyone mode; contacts-only falls back to Everyone",
                    "airdrop.receive_mode",
                )
            )
        if interface:
            try:
                hardware = detect_hardware(self.runtime)
                names = {item.name for item in hardware.interfaces}
            except Exception:
                names = set()
            if names and interface not in names:
                issues.append(
                    ValidationIssue(
                        IssueLevel.WARNING,
                        f"AirDrop interface {interface} is not present right now",
                        "airdrop.interface",
                    )
                )
        return issues

    def render(self, config: AppConfig) -> list[RenderedFile]:
        interface = self.interface(config)
        content = render_template(
            "modules/airdrop/radio.toml.j2",
            station=interface,
            allow_untested=config.airdrop.allow_untested_hardware,
            reconnect_after=config.airdrop.reconnect_after,
            country=config.hotspot.country_code,
        )
        return [
            RenderedFile(
                Path("/etc/owly/radio.toml"),
                content,
                mode=0o644,
                owner="root",
                group="root",
            )
        ]

    def apply(self) -> ActionResult:
        cfg = self.airdrop
        if not cfg.enabled:
            return ActionResult.success("AirDrop disabled")
        binary = cfg.owl_binary
        if not self.runtime.fs.exists(binary) and self.runtime.runner.which("owly") is None:
            return ActionResult.failure(
                "owly is not installed; install it from https://github.com/t4t5/owly "
                "(Rust 1.88+, no arm64 release asset: build from source)"
            )
        target = self.target_dir()
        self.runtime.fs.mkdir(target, mode=0o770)
        self.runtime.fs.mkdir(Path("/var/lib/eventhub-airdrop"), mode=0o750)
        with contextlib.suppress(KeyError, OSError):
            self.runtime.fs.chown(
                target,
                self.config.install.airdrop_user,
                self.config.install.service_group,
            )
        if self.runtime.systemd.is_enabled("owlyd.service"):
            if not self.runtime.systemd.is_active("owlyd.service"):
                self.runtime.systemd.start("owlyd.service")
        else:
            self.runtime.systemd.enable("owlyd.service")
            self.runtime.systemd.start("owlyd.service")
        if not self.runtime.systemd.restart("eventhub-radio.service"):
            return ActionResult.failure(
                "could not start the owly radio; run `owly doctor` on the Pi"
            )
        if not self.runtime.systemd.restart("eventhub-airdrop.service"):
            return ActionResult.failure(
                "could not start owly receive; check journalctl -u eventhub-airdrop"
            )
        self.runtime.bus.emit(
            "airdrop.started",
            {"interface": self.interface(), "target": str(target)},
            source="airdrop",
        )
        return ActionResult.success(f"AirDrop receiving into {target}")

    def stop(self) -> ActionResult:
        result = ActionResult.success("AirDrop stopped")
        for unit in ("eventhub-airdrop.service", "eventhub-radio.service"):
            if not self.runtime.systemd.stop(unit):
                result = ActionResult.failure(f"could not stop {unit}")
        return result

    def health_check(self) -> HealthResult:
        if not self.airdrop.enabled:
            return HealthResult(True, "AirDrop disabled")
        units = {
            "owlyd.service": self.runtime.systemd.is_active("owlyd.service"),
            "eventhub-radio.service": self.runtime.systemd.is_active(
                "eventhub-radio.service"
            ),
            "eventhub-airdrop.service": self.runtime.systemd.is_active(
                "eventhub-airdrop.service"
            ),
        }
        inactive = [unit for unit, active in units.items() if not active]
        if inactive:
            return HealthResult(False, "inactive: " + ", ".join(inactive))
        iface = self.airdrop.awdl_interface
        if not self.runtime.fs.exists(Path("/sys/class/net") / iface):
            return HealthResult(False, f"{iface} is missing")
        return HealthResult(
            True,
            f"receiving on {iface}",
            {"interface": iface, "target": str(self.target_dir())},
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

    def doctor(self) -> dict[str, Any]:
        binary = str(self.airdrop.owl_binary)
        if not self.runtime.fs.exists(Path(binary)) and self.runtime.runner.which("owly"):
            binary = str(self.runtime.runner.which("owly"))
        result = self.runtime.runner.run([binary, "doctor", "--json"], timeout=30)
        if not result.ok:
            return {"ok": False, "error": result.stderr.strip() or result.stdout.strip()}
        try:
            payload = json.loads(result.stdout)
        except ValueError:
            return {"ok": False, "error": "owly doctor did not return JSON"}
        return payload if isinstance(payload, dict) else {"ok": False, "raw": payload}


SERVICE = AirdropService
