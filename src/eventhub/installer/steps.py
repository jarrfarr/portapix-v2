"""Installer steps.

Each step is idempotent, returns a structured result, and may implement a
rollback.  Steps receive a context with the injected runtime, so the whole
installer can be exercised against fake OS adapters.
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from eventhub.core.config import AppConfig, config_yaml, merge_config
from eventhub.core.context import Runtime
from eventhub.core.db import utcnow
from eventhub.core.logging import log_marker
from eventhub.core.passwords import copyparty_hash, hash_password
from eventhub.core.types import RenderedFile
from eventhub.installer.distro import (
    Distro,
    PackageManager,
    detect_distro,
    package_commands,
)
from eventhub.installer.packages import packages_for_modules
from eventhub.installer.units import render_units, units_to_enable
from eventhub.network.hardware import HardwareReport, detect_hardware
from eventhub.registry import ServiceRegistry

StepState = Literal["ok", "warn", "fail", "skip"]


@dataclass(frozen=True, slots=True)
class StepResult:
    step: str
    state: StepState
    message: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.state != "fail"


@dataclass(slots=True)
class InstallOptions:
    unattended: bool = False
    dry_run: bool = False
    skip_packages: bool = False
    skip_airdrop_build: bool = False
    start_services: bool = True
    run_selftest: bool = False
    answers: dict[str, object] | None = None


@dataclass(slots=True)
class InstallContext:
    runtime: Runtime
    options: InstallOptions
    config: AppConfig
    progress: Callable[[str, str, str], None] = lambda *args: None
    results: list[StepResult] = field(default_factory=list)
    distro: Distro | None = None
    hardware: HardwareReport | None = None
    admin_password: str | None = None
    admin_username: str = "admin"
    rendered_units: list[RenderedFile] = field(default_factory=list)
    enabled_units: list[str] = field(default_factory=list)
    rollbacks: list[tuple[str, Callable[[], None]]] = field(default_factory=list)

    @property
    def dry_run(self) -> bool:
        return self.options.dry_run

    def log(self, level: str, message: str) -> None:
        log_marker(self.runtime.logger, level, message)

    def save_config(self) -> None:
        text = config_yaml(self.config)
        self.runtime.fs.atomic_write(self.runtime.config_path, text, mode=0o600)
        self.runtime.config = self.config

    def available_modules(self) -> list[str]:
        return ServiceRegistry(self.runtime).names()

    def enabled_modules(self) -> list[str]:
        return [name for name in self.available_modules() if self.config.module_enabled(name)]


class InstallStep(ABC):
    id: str
    title: str
    description: str = ""
    critical: bool = True

    @abstractmethod
    def run(self, ctx: InstallContext) -> StepResult: ...

    def rollback(self, ctx: InstallContext) -> None:  # pragma: no cover - optional
        return None


class DetectSystemStep(InstallStep):
    id = "detect"
    title = "Detect hardware and operating system"
    description = "Identify the Pi model, network adapters, storage and package manager."

    def run(self, ctx: InstallContext) -> StepResult:
        distro = detect_distro(ctx.runtime.fs)
        ctx.distro = distro
        try:
            hardware = detect_hardware(ctx.runtime)
            ctx.hardware = hardware
        except Exception as exc:
            hardware = HardwareReport()
            ctx.hardware = hardware
            ctx.log("warn", f"hardware detection failed: {exc}")
        notes = list(distro.notes) + list(hardware.warnings)
        for note in notes:
            ctx.log("warn", note)
        if not distro.supported and not ctx.options.skip_packages:
            return StepResult(
                self.id,
                "fail",
                f"unsupported distribution: {distro.name}",
                detail="; ".join(notes),
            )
        detail = (
            f"model={hardware.model}; ap={hardware.suggested_roles.get('ap')}; "
            f"airdrop={hardware.suggested_roles.get('airdrop')}; "
            f"wan={hardware.suggested_roles.get('wan')}; lan={hardware.suggested_roles.get('lan')}"
        )
        return StepResult(
            self.id,
            "warn" if notes else "ok",
            f"{distro.name} on {hardware.model}",
            detail=detail,
        )

    def apply_suggestions(self, ctx: InstallContext) -> None:
        """Apply detected interfaces for roles the operator left on auto."""

        if ctx.hardware is None:
            return
        roles = ctx.config.roles
        suggested = ctx.hardware.suggested_roles
        update: dict[str, str] = {}
        for role in ("ap", "airdrop", "wan", "lan"):
            current = getattr(roles, role)
            if current == "auto" and suggested.get(role) not in {None, "auto"}:
                update[role] = str(suggested[role])
        if update:
            ctx.config = merge_config(ctx.config, {"roles": update})
        if ctx.config.hotspot.interface == "auto" and update.get("ap"):
            ctx.config = merge_config(
                ctx.config, {"hotspot": {"interface": update["ap"]}}
            )
        if ctx.config.airdrop.interface == "auto" and update.get("airdrop"):
            ctx.config = merge_config(
                ctx.config, {"airdrop": {"interface": update["airdrop"]}}
            )


class PrepareFilesystemStep(InstallStep):
    id = "dirs"
    title = "Create directories and permissions"
    description = "Data, state, logs, config, runtime socket and ingest folders."

    def run(self, ctx: InstallContext) -> StepResult:
        config = ctx.config
        directories: list[tuple[Path, int]] = [
            (config.data_root, 0o750),
            (config.state_dir, 0o750),
            (config.log_dir, 0o750),
            (config.events_root, 0o750),
            (config.ingest_root, 0o770),
            (ctx.runtime.config_path.parent, 0o750),
            (config.state_dir / "backups", 0o700),
            (config.state_dir / "snapshots", 0o700),
            (config.fileserver_copyparty.home_dir, 0o750),
            (config.helper.socket_path.parent, 0o750),
            (config.install.install_root, 0o755),
        ]
        if config.airdrop.target_dir is not None:
            directories.append((config.airdrop.target_dir, 0o770))
        for path, mode in directories:
            if not ctx.dry_run:
                ctx.runtime.fs.mkdir(path, mode=mode)
        return StepResult(self.id, "ok", f"prepared {len(directories)} directories")


class WriteConfigStep(InstallStep):
    id = "config"
    title = "Write validated configuration"
    description = "Persist the single source of truth as YAML mode 0600."

    def run(self, ctx: InstallContext) -> StepResult:
        if ctx.options.answers:
            ctx.config = merge_config(ctx.config, ctx.options.answers)
        if not ctx.dry_run:
            ctx.save_config()
        rendered = config_yaml(ctx.config)
        digest = f"{len(rendered)} bytes"
        return StepResult(self.id, "ok", f"configuration written ({digest})")


class CreateServiceAccountsStep(InstallStep):
    id = "accounts"
    title = "Create service accounts"
    description = "Unprivileged dashboard, file server, helper and AirDrop users."

    def _group(self, ctx: InstallContext, name: str) -> None:
        result = ctx.runtime.runner.run(["getent", "group", name], timeout=10)
        if result.returncode != 0:
            run = ctx.runtime.runner.run(["groupadd", "--system", name], timeout=30)
            if not run.ok:
                raise RuntimeError(run.stderr.strip() or f"groupadd {name} failed")
            ctx.log("ok", f"created group {name}")

    def _user(self, ctx: InstallContext, name: str, group: str) -> None:
        result = ctx.runtime.runner.run(["getent", "passwd", name], timeout=10)
        if result.returncode == 0:
            return
        install_root = str(ctx.config.install.install_root)
        run = ctx.runtime.runner.run(
            [
                "useradd",
                "--system",
                "--gid",
                group,
                "--home-dir",
                install_root,
                "--no-create-home",
                "--shell",
                "/usr/sbin/nologin",
                name,
            ],
            timeout=30,
        )
        if not run.ok:
            raise RuntimeError(run.stderr.strip() or f"useradd {name} failed")
        ctx.log("ok", f"created user {name}")

    def run(self, ctx: InstallContext) -> StepResult:
        install = ctx.config.install
        users = [
            (install.service_user, install.service_group),
            (install.dashboard_user, install.service_group),
            (install.airdrop_user, install.service_group),
        ]
        if ctx.dry_run:
            return StepResult(self.id, "ok", "would create service accounts")
        try:
            self._group(ctx, install.service_group)
            self._group(ctx, ctx.config.users.default_group)
            for name, group in users:
                self._user(ctx, name, group)
        except (RuntimeError, OSError) as exc:
            return StepResult(self.id, "fail", str(exc))
        return StepResult(self.id, "ok", "service accounts ready")


class InstallPackagesStep(InstallStep):
    id = "packages"
    title = "Install system packages"
    description = "hostapd, dnsmasq, nftables, Samba, Avahi, build tools and storage tools."

    def run(self, ctx: InstallContext) -> StepResult:
        if ctx.options.skip_packages:
            return StepResult(self.id, "skip", "package installation skipped by request")
        if ctx.distro is None:
            return StepResult(self.id, "fail", "distribution was not detected")
        manager = ctx.distro.package_manager
        if manager is PackageManager.UNKNOWN:
            return StepResult(self.id, "fail", "no supported package manager")
        binary = "apt-get" if manager is PackageManager.APT else manager.value
        if ctx.runtime.runner.which(binary) is None:
            return StepResult(self.id, "fail", f"{binary} is not available")
        logical = packages_for_modules(ctx.enabled_modules())
        commands = package_commands(manager, logical, ctx.distro.family)
        if not commands:
            return StepResult(self.id, "skip", "no packages required")
        if ctx.dry_run:
            return StepResult(self.id, "ok", f"would install {len(logical)} package groups")
        for command in commands:
            result = ctx.runtime.runner.run(
                command.argv, timeout=1800, env=command.env
            )
            if not result.ok:
                return StepResult(
                    self.id,
                    "fail",
                    f"{command.description} failed",
                    detail=result.stderr[-2000:],
                )
        return StepResult(self.id, "ok", f"installed {len(logical)} package groups")


class InstallPythonEnvironmentStep(InstallStep):
    id = "python"
    title = "Install EventHub into its virtualenv"
    description = "Create /opt/eventhub/venv and install the pinned package."

    def _project_root(self) -> Path | None:
        import eventhub

        package = Path(eventhub.__file__).resolve()
        # src/eventhub/__init__.py -> repository root
        candidate = package.parents[2]
        return candidate if (candidate / "pyproject.toml").exists() else None

    def run(self, ctx: InstallContext) -> StepResult:
        install = ctx.config.install
        venv_python = install.venv_path / "bin" / "python"
        entry = install.venv_path / "bin" / "eventhub"
        if ctx.runtime.fs.exists(entry):
            return StepResult(self.id, "ok", "virtualenv already installed", str(entry))
        if ctx.dry_run:
            return StepResult(self.id, "ok", "would create the virtualenv")
        if not ctx.runtime.fs.exists(venv_python):
            result = ctx.runtime.runner.run(
                ["python3", "-m", "venv", str(install.venv_path)], timeout=300
            )
            if not result.ok:
                return StepResult(
                    self.id, "fail", "could not create virtualenv", result.stderr[-2000:]
                )
        project = self._project_root()
        source = str(project) if project is not None else str(install.install_root)
        pip = install.venv_path / "bin" / "pip"
        result = ctx.runtime.runner.run(
            [str(pip), "install", "--upgrade", "pip"], timeout=600
        )
        if not result.ok:
            ctx.log("warn", f"pip self-upgrade failed: {result.stderr[-500:]}")
        result = ctx.runtime.runner.run(
            [str(pip), "install", "--upgrade", f"{source}[copyparty]"], timeout=1800
        )
        if not result.ok:
            result = ctx.runtime.runner.run(
                [str(pip), "install", "--upgrade", source], timeout=1800
            )
        if not result.ok:
            return StepResult(
                self.id, "fail", "pip install failed", result.stderr[-2000:]
            )
        return StepResult(self.id, "ok", f"installed from {source}")


class FirstBootAdminStep(InstallStep):
    id = "admin"
    title = "Create the first administrator"
    description = "Generate a strong password, hash it with argon2 and write it to a file."

    def run(self, ctx: InstallContext) -> StepResult:
        db = ctx.runtime.db
        existing = db.scalar("SELECT COUNT(*) FROM users WHERE role = 'admin'")
        if existing:
            return StepResult(self.id, "skip", "an administrator already exists")
        from eventhub.core.config import generate_secret

        password = ctx.admin_password or generate_secret(20)
        ctx.admin_password = password
        password_hash = hash_password(password)
        cp_salt = ctx.config.fileserver_copyparty.password_salt.get_secret_value()
        cp_hash = copyparty_hash(password, cp_salt)
        now = utcnow()
        username = ctx.admin_username
        if ctx.dry_run:
            return StepResult(self.id, "ok", "would create administrator account")
        db.execute(
            """
            INSERT INTO users(username, display_name, password_hash, copyparty_hash,
                              role, enabled, must_change_password, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'admin', 1, 1, ?, ?)
            """,
            (username, "Administrator", password_hash, cp_hash, now, now),
        )
        try:
            registry = ServiceRegistry(ctx.runtime)
            if registry.is_available("fileserver_smb") and ctx.config.fileserver_smb.enabled:
                synced = registry.get("fileserver_smb").sync_user(username, password)
                if not synced.ok:
                    ctx.log("warn", f"SMB admin sync failed: {synced.message}")
        except Exception as exc:
            ctx.log("warn", f"SMB admin sync failed: {exc}")
        content = (
            "EventHub first-boot administrator\n"
            "=================================\n"
            f"URL:      http://{ctx.config.discovery.hostname}.local/\n"
            f"Address:  http://{ctx.config.hotspot.server_ip}/\n"
            f"Username: {username}\n"
            f"Password: {password}\n\n"
            "You must change this password when you first log in.\n"
            "Delete this file after logging in.\n"
        )
        path = ctx.config.dashboard.first_boot_password_file
        ctx.runtime.fs.atomic_write(path, content, mode=0o600)
        ctx.log("warn", f"first-boot admin password written to {path}")
        return StepResult(
            self.id,
            "ok",
            f"administrator {username!r} created",
            detail=f"password file: {path}",
        )


class RenderUnitsStep(InstallStep):
    id = "units"
    title = "Install systemd units"
    description = "Write, daemon-reload and enable units for available modules."

    def run(self, ctx: InstallContext) -> StepResult:
        registry = ServiceRegistry(ctx.runtime)
        modules = [name for name in ctx.enabled_modules() if registry.is_available(name)]
        rendered = render_units(ctx.config, modules, ctx.distro)
        ctx.rendered_units = rendered
        if not ctx.dry_run:
            for item in rendered:
                ctx.runtime.fs.atomic_write(item.path, item.content, mode=item.mode)
            ctx.runtime.systemd.daemon_reload()
            enabled: list[str] = []
            for unit in units_to_enable(ctx.config, modules, ctx.distro):
                if ctx.runtime.systemd.enable(unit):
                    enabled.append(unit)
            ctx.enabled_units = enabled
        return StepResult(
            self.id,
            "ok" if rendered or ctx.dry_run else "warn",
            f"installed {len(rendered)} unit files, enabled "
            f"{len(ctx.enabled_units) if not ctx.dry_run else 'several'} units",
        )


class ApplyConfigurationStep(InstallStep):
    id = "render"
    title = "Validate and render configuration"
    description = "Write hostapd, dnsmasq, nftables, Samba, copyparty, Avahi configs."

    def run(self, ctx: InstallContext) -> StepResult:
        registry = ServiceRegistry(ctx.runtime)
        result = registry.apply(dry_run=ctx.dry_run, reload_services=False)
        ctx.results.extend(
            StepResult(f"render.{issue.field or 'general'}", "warn", issue.message)
            for issue in result.issues
        )
        if not result.ok:
            return StepResult(self.id, "fail", result.message)
        return StepResult(
            self.id, "ok", f"validated and rendered {len(result.applied)} modules"
        )


class StartServicesStep(InstallStep):
    id = "start"
    title = "Start services"
    description = "Apply runtime rules and start every enabled unit."

    critical = False

    def run(self, ctx: InstallContext) -> StepResult:
        if not ctx.options.start_services:
            return StepResult(self.id, "skip", "service start skipped by request")
        registry = ServiceRegistry(ctx.runtime)
        result = registry.apply(dry_run=ctx.dry_run, reload_services=True)
        if not result.ok:
            return StepResult(self.id, "fail", result.message)
        failures: list[str] = []
        started: list[str] = []
        for name in ctx.enabled_modules():
            service = registry.get(name)
            outcome = service.start()
            if outcome.ok:
                started.append(name)
            else:
                failures.append(f"{name}: {outcome.message}")
        if failures:
            return StepResult(
                self.id,
                "warn",
                f"started {len(started)} modules; {len(failures)} failed",
                detail="; ".join(failures),
            )
        return StepResult(self.id, "ok", f"started {len(started)} modules")


class SelfTestStep(InstallStep):
    id = "selftest"
    title = "Run self-test"
    description = "Check every service, port, mount and route."

    def run(self, ctx: InstallContext) -> StepResult:
        if not ctx.options.run_selftest:
            return StepResult(self.id, "skip", "self-test not requested")
        from eventhub.system.selftest import SelfTest

        report = SelfTest(ctx.runtime).run()
        if report.ok:
            return StepResult(self.id, "ok", "self-test passed")
        return StepResult(self.id, "warn", "self-test reported problems", report.summary())


def standard_steps() -> list[InstallStep]:
    return [
        DetectSystemStep(),
        PrepareFilesystemStep(),
        WriteConfigStep(),
        CreateServiceAccountsStep(),
        InstallPackagesStep(),
        InstallPythonEnvironmentStep(),
        FirstBootAdminStep(),
        RenderUnitsStep(),
        ApplyConfigurationStep(),
        StartServicesStep(),
        SelfTestStep(),
    ]


def copy_file(fs: object, source: Path, target: Path, mode: int = 0o644) -> None:
    """Small helper used by backup/restore in tests."""

    text = shutil.os.fsdecode(source)
    del text, fs, target, mode
