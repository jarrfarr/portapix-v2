"""Installer, updater, repair and uninstall orchestration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from eventhub.core.config import ConfigError, merge_config
from eventhub.core.context import Runtime
from eventhub.core.errors import EventHubError
from eventhub.core.types import ActionResult, RenderedFile
from eventhub.installer.backup import BackupManager
from eventhub.installer.distro import (
    PackageManager,
    detect_distro,
    package_commands,
)
from eventhub.installer.packages import MODULE_PACKAGES
from eventhub.installer.steps import (
    DetectSystemStep,
    InstallContext,
    InstallOptions,
    StepResult,
    standard_steps,
)
from eventhub.installer.units import render_units, unit_names
from eventhub.registry import ServiceRegistry

SAFE_MODES = {"services-only", "keep-data", "full-wipe"}
FULL_WIPE_CONFIRMATION = "FULL WIPE"


@dataclass(frozen=True, slots=True)
class InstallResult:
    ok: bool
    message: str
    steps: tuple[StepResult, ...] = ()
    admin_password: str | None = None
    warnings: tuple[str, ...] = ()
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "admin_password": self.admin_password,
            "warnings": list(self.warnings),
            "dry_run": self.dry_run,
            "steps": [
                {"step": s.step, "state": s.state, "message": s.message, "detail": s.detail}
                for s in self.steps
            ],
        }


@dataclass(slots=True)
class UninstallSummary:
    mode: str
    removed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)


ProgressCallback = Callable[[str, str, str], None]


class Installer:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.log = runtime.logger.getChild("installer")

    # -- helpers -------------------------------------------------------
    def _load_answers(self, path: Path | str | None) -> dict[str, Any]:
        if path is None:
            return {}
        try:
            raw = yaml.safe_load(self.runtime.fs.read_text(path))
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigError(f"could not read answers file {path}: {exc}") from exc
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            raise ConfigError("answers file root must be a mapping")
        return dict(raw)

    def _config_backup(self) -> tuple[bool, str]:
        path = self.runtime.config_path
        if self.runtime.fs.exists(path):
            return True, self.runtime.fs.read_text(path)
        return False, ""

    def _restore_config_backup(self, existed: bool, text: str) -> None:
        if existed:
            self.runtime.fs.atomic_write(self.runtime.config_path, text, mode=0o600)
        else:
            self.runtime.fs.remove(self.runtime.config_path, missing_ok=True)

    def _auto_adjust(self, ctx: InstallContext, detect: DetectSystemStep) -> None:
        detect.apply_suggestions(ctx)
        hardware = ctx.hardware
        if hardware is None:
            return
        if (
            hardware.interfaces
            and not hardware.airdrop_supported
            and ctx.config.airdrop.enabled
        ):
            ctx.config = merge_config(ctx.config, {"airdrop": {"enabled": False}})
            ctx.log(
                "warn",
                "AirDrop disabled: no USB wifi adapter with monitor-mode support was "
                "detected. Attach a supported adapter and enable it in Modules.",
            )
        if ctx.config.hotspot.interface == "auto" and ctx.config.roles.ap not in {
            "auto",
            "none",
        }:
            ctx.config = merge_config(
                ctx.config, {"hotspot": {"interface": ctx.config.roles.ap}}
            )
        storage = ctx.config.storage
        if (
            storage.enabled
            and not (storage.drive.uuid or storage.drive.device or storage.drive.label)
            and hardware.drives
        ):
            drive = next((d for d in hardware.drives if d.removable), hardware.drives[0])
            if drive.uuid:
                ctx.config = merge_config(
                    ctx.config, {"storage": {"drive": {"uuid": drive.uuid}}}
                )
                ctx.log(
                    "ok",
                    f"selected storage drive {drive.label or drive.uuid}",
                )

    # -- install -------------------------------------------------------
    def install(
        self,
        *,
        unattended: bool = False,
        answers_path: Path | str | None = None,
        dry_run: bool = False,
        start_services: bool = True,
        skip_packages: bool = False,
        run_selftest: bool = False,
        progress: ProgressCallback | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> InstallResult:
        if not self.runtime.privileged and not dry_run:
            return InstallResult(False, "install requires root")
        try:
            answers = self._load_answers(answers_path)
        except ConfigError as exc:
            return InstallResult(False, str(exc))
        options = InstallOptions(
            unattended=unattended,
            dry_run=dry_run,
            skip_packages=skip_packages,
            start_services=start_services,
            run_selftest=run_selftest,
            answers=answers,
        )
        config = self.runtime.config
        if answers:
            try:
                config = merge_config(config, answers)
            except ConfigError as exc:
                return InstallResult(False, str(exc))
        ctx = InstallContext(
            runtime=self.runtime,
            options=options,
            config=config,
            progress=progress or (lambda *args: None),
        )
        existed, original = self._config_backup()
        warnings: list[str] = []
        steps = standard_steps()
        detect_step = next(step for step in steps if isinstance(step, DetectSystemStep))
        for step in steps:
            if cancel is not None and cancel():
                self._restore_config_backup(existed, original)
                ctx.progress(step.id, "fail", "cancelled")
                return InstallResult(
                    False,
                    "install cancelled; configuration rolled back",
                    tuple(ctx.results),
                    warnings=tuple(warnings),
                    dry_run=dry_run,
                )
            self.log.info("install step %s: %s", step.id, step.title)
            ctx.progress(step.id, "start", step.title)
            try:
                result = step.run(ctx)
            except Exception as exc:
                self.log.exception("install step %s crashed", step.id)
                result = StepResult(step.id, "fail", f"step crashed: {exc}")
            if step.id == detect_step.id:
                self._auto_adjust(ctx, detect_step)
            ctx.results.append(result)
            level = {"ok": "ok", "warn": "warn", "fail": "fail", "skip": "info"}[
                result.state
            ]
            ctx.log(level, result.message)
            ctx.progress(step.id, result.state, result.message)
            if result.state == "warn":
                warnings.append(result.message)
            if not result.ok and step.critical:
                self._restore_config_backup(existed, original)
                ctx.progress(step.id, "fail", "install aborted and configuration rolled back")
                return InstallResult(
                    False,
                    f"install failed at {step.title}: {result.message}",
                    tuple(ctx.results),
                    warnings=tuple(warnings),
                    dry_run=dry_run,
                )
        self.runtime.db.audit(
            "install.complete",
            actor="installer",
            detail=f"version={ctx.config.version} dry_run={dry_run}",
        )
        return InstallResult(
            True,
            "dry run complete; nothing was changed"
            if dry_run
            else "EventHub is installed and running",
            tuple(ctx.results),
            admin_password=None if dry_run else ctx.admin_password,
            warnings=tuple(warnings),
            dry_run=dry_run,
        )

    # -- module reinstall ---------------------------------------------
    def reinstall_module(self, name: str) -> ActionResult:
        if not self.runtime.privileged:
            return ActionResult.failure("reinstall requires root")
        registry = ServiceRegistry(self.runtime)
        if not registry.is_available(name):
            return ActionResult.failure(f"module not available: {name}")
        distro = detect_distro(self.runtime.fs)
        if distro.package_manager is not PackageManager.UNKNOWN:
            logical = MODULE_PACKAGES.get(name, [])
            if logical:
                for command in package_commands(
                    distro.package_manager, logical, distro.family
                ):
                    result = self.runtime.runner.run(
                        command.argv, timeout=1800, env=command.env
                    )
                    if not result.ok:
                        return ActionResult.failure(
                            f"package install failed: {result.stderr[-500:]}"
                        )
        context = InstallContext(
            runtime=self.runtime, options=InstallOptions(), config=self.runtime.config
        )
        service = registry.get(name)
        rendered: list[RenderedFile] = render_units(self.runtime.config, [name], distro)
        for item in rendered:
            self.runtime.fs.atomic_write(item.path, item.content, mode=item.mode)
        if rendered:
            self.runtime.systemd.daemon_reload()
            for unit in unit_names([name], distro):
                self.runtime.systemd.enable(unit)
        result = registry.apply(names=[name], reload_services=True)
        if not result.ok:
            return ActionResult.failure(result.message)
        outcome = service.restart()
        service.mark_installed()
        context.log("ok", f"reinstalled {name}")
        return outcome if outcome.ok else ActionResult.success(f"{name} reinstalled")

    # -- module removal -----------------------------------------------
    def remove_module(self, name: str) -> ActionResult:
        """Disable a module, stop it and remove EventHub-owned unit files.

        System packages and user data are left in place; this is the safe
        "remove the service" action behind the TUI Modules screen.
        """

        if not self.runtime.privileged:
            return ActionResult.failure("removing a module requires root")
        registry = ServiceRegistry(self.runtime)
        if not registry.is_available(name):
            return ActionResult.failure(f"module not available: {name}")
        service = registry.get(name)
        try:
            service.stop()
        except Exception:
            self.log.warning("could not stop %s", name)
        distro = detect_distro(self.runtime.fs)
        for unit in unit_names([name], distro):
            self.runtime.systemd.disable(unit)
            if unit.startswith("eventhub-"):
                self.runtime.fs.remove(Path("/etc/systemd/system") / unit, missing_ok=True)
        self.runtime.systemd.daemon_reload()
        try:
            config = merge_config(
                self.runtime.config, {service.config_key: {"enabled": False}}
            )
            self.save_config(config)
        except ConfigError as exc:
            return ActionResult.failure(str(exc))
        self.runtime.db.audit("module.remove", target=name)
        return ActionResult.success(f"removed {name} (packages and data kept)")

    def save_config(self, config: object) -> None:
        from eventhub.core.config import AppConfig, config_yaml

        assert isinstance(config, AppConfig)
        self.runtime.fs.atomic_write(
            self.runtime.config_path, config_yaml(config), mode=0o600
        )
        self.runtime.config = config

    # -- update --------------------------------------------------------
    def _repo_path(self) -> Path | None:
        candidates = [
            self.runtime.config.install.install_root,
            self.runtime.config.install.install_root / "repo",
            Path.cwd(),
        ]
        for candidate in candidates:
            if self.runtime.fs.exists(candidate / ".git"):
                return candidate
        return None

    def update(self, *, check_only: bool = False, yes: bool = False) -> ActionResult:
        if not check_only and not self.runtime.privileged:
            return ActionResult.failure("update requires root")
        repo = self._repo_path()
        if repo is None:
            return ActionResult.failure(
                "no git checkout found; re-run bootstrap.sh to install/update"
            )
        branch = self.runtime.config.system.update_branch
        current = self.runtime.runner.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], timeout=20
        )
        if not current.ok:
            return ActionResult.failure(f"git rev-parse failed: {current.stderr.strip()}")
        local = current.stdout.strip()
        remote = self.runtime.runner.run(
            ["git", "-C", str(repo), "ls-remote", "origin", f"refs/heads/{branch}"],
            timeout=60,
        )
        if not remote.ok:
            return ActionResult.failure(
                f"could not reach the update remote: {remote.stderr.strip()[:300]}"
            )
        parts = remote.stdout.split()
        available = parts[0] if parts else ""
        if check_only:
            if not available or available == local:
                return ActionResult.success("EventHub is up to date", local=local)
            return ActionResult.success(
                "an update is available", local=local, remote=available
            )
        if not yes:
            return ActionResult.failure("update requires --yes or the TUI confirmation")
        if available and available == local:
            return ActionResult.success("EventHub is already up to date", local=local)
        backup = BackupManager(self.runtime).export(include_events=False)
        self.log.info("update backup at %s", backup.path)
        pull = self.runtime.runner.run(
            ["git", "-C", str(repo), "pull", "--ff-only", "origin", branch], timeout=300
        )
        if not pull.ok:
            return ActionResult.failure(f"git pull failed: {pull.stderr[-1000:]}")
        install = self.runtime.runner.run(
            [
                str(self.runtime.config.install.venv_path / "bin" / "pip"),
                "install",
                "--upgrade",
                f"{repo}[copyparty]",
            ],
            timeout=1800,
        )
        registry = ServiceRegistry(self.runtime)
        applied = registry.apply()
        if not pull.ok or not install.ok or not applied.ok:
            reason = (
                applied.message
                if not applied.ok
                else install.stderr[-1000:]
                if not install.ok
                else pull.stderr[-1000:]
            )
            self._rollback_update(repo, local, backup.path)
            return ActionResult.failure(f"update failed and was rolled back: {reason}")
        for name in registry.names():
            registry.get(name).start()
        self.runtime.db.audit(
            "update.complete", actor="installer", detail=f"{local} -> {available}"
        )
        return ActionResult.success(
            f"updated {local[:8]} -> {(available or local)[:8]}",
            backup=str(backup.path),
        )

    def _rollback_update(self, repo: Path, commit: str, backup_path: Path) -> None:
        self.log.warning("rolling update back to %s", commit[:8])
        self.runtime.runner.run(
            ["git", "-C", str(repo), "reset", "--hard", commit], timeout=120
        )
        self.runtime.runner.run(
            [
                str(self.runtime.config.install.venv_path / "bin" / "pip"),
                "install",
                "--upgrade",
                f"{repo}[copyparty]",
            ],
            timeout=1800,
        )
        try:
            BackupManager(self.runtime).restore(backup_path, actor="installer")
        except EventHubError:
            self.log.exception("could not restore config backup during rollback")
        ServiceRegistry(self.runtime).apply()

    # -- uninstall -----------------------------------------------------
    def uninstall(
        self, *, mode: str = "services-only", yes: bool = False, confirm: str = ""
    ) -> ActionResult:
        if not self.runtime.privileged:
            return ActionResult.failure("uninstall requires root")
        if mode not in SAFE_MODES:
            return ActionResult.failure(
                f"unknown mode {mode!r}; expected {', '.join(sorted(SAFE_MODES))}"
            )
        if not yes:
            return ActionResult.failure("uninstall requires explicit --yes confirmation")
        if mode == "full-wipe" and confirm != FULL_WIPE_CONFIRMATION:
            return ActionResult.failure(
                f"full wipe requires --confirm {FULL_WIPE_CONFIRMATION!r}"
            )
        summary = UninstallSummary(mode=mode)
        registry = ServiceRegistry(self.runtime)
        distro = detect_distro(self.runtime.fs)
        for name in registry.names():
            try:
                registry.get(name).stop()
            except Exception:
                self.log.warning("could not stop %s", name)
        for unit in unit_names(registry.names(), distro):
            self.runtime.systemd.disable(unit)
            if unit.startswith("eventhub-"):
                self.runtime.fs.remove(Path("/etc/systemd/system") / unit, missing_ok=True)
        self.runtime.systemd.daemon_reload()
        summary.removed.append("systemd units and services")
        config = self.runtime.config
        if mode in {"keep-data", "full-wipe"}:
            removable = [
                (self.runtime.config_path, "configuration"),
                (config.state_dir, "state, users and sessions"),
                (config.log_dir, "logs"),
                (config.install.install_root, "virtualenv and application"),
            ]
            for path, label in removable:
                self._safe_remove(path)
                summary.removed.append(label)
            summary.kept.extend([str(config.data_root)])
            if mode == "full-wipe":
                targets = [config.data_root]
                if config.events_root not in {config.data_root, config.data_root / "events"}:
                    targets.append(config.events_root)
                for path in targets:
                    self._safe_remove(path)
                    summary.removed.append(f"event data: {path}")
                summary.kept.clear()
            # Remove service accounts last, best effort.
            for user in {
                config.install.dashboard_user,
                config.install.airdrop_user,
                config.install.service_user,
            }:
                self.runtime.runner.run(["userdel", user], timeout=30)
            self.runtime.runner.run(
                ["groupdel", config.install.service_group], timeout=30
            )
        else:
            summary.kept.extend(
                [str(self.runtime.config_path), str(config.state_dir), str(config.data_root)]
            )
        self.log.info(
            "uninstall mode=%s removed=%s kept=%s", mode, summary.removed, summary.kept
        )
        return ActionResult(
            True,
            f"uninstall ({mode}) complete",
            details={"removed": summary.removed, "kept": summary.kept},
        )

    def _safe_remove(self, path: Path) -> None:
        text = str(path)
        if text in {"/", "", "."} or not path.is_absolute():
            raise EventHubError(f"refusing to remove unsafe path: {path}")
        if len(path.parts) < 2:
            raise EventHubError(f"refusing to remove top-level path: {path}")
        self.runtime.fs.rmtree(path, missing_ok=True)

    # -- status --------------------------------------------------------
    def status(self) -> dict[str, Any]:
        registry = ServiceRegistry(self.runtime)
        return {
            "installed": self.runtime.fs.exists(self.runtime.config_path),
            "config_path": str(self.runtime.config_path),
            "modules": {name: status.as_dict() for name, status in registry.statuses().items()},
        }
