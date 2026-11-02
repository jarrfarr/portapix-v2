"""Thin TUI controller.

The Textual screens contain no business logic: they call these methods, which
in turn call the installer, registry, backup manager and system module.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import qrcode
import yaml

from eventhub.core.adapters import is_root
from eventhub.core.config import (
    AppConfig,
    ConfigError,
    config_yaml,
    default_config,
    load_config,
    merge_config,
)
from eventhub.core.context import Runtime, build_runtime
from eventhub.core.db import Database, open_database
from eventhub.core.errors import EventHubError
from eventhub.core.logging import tail_file
from eventhub.core.types import ActionResult, ApplyResult, ServiceStatus
from eventhub.installer.backup import BackupInfo, BackupManager
from eventhub.installer.manager import Installer
from eventhub.network.hardware import HardwareReport, detect_hardware
from eventhub.registry import ServiceRegistry

Progress = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class OpResult:
    ok: bool
    message: str
    payload: Any = None


def qr_ascii(data: str) -> str:
    """Render a compact QR code using half-height Unicode blocks."""

    qr = qrcode.QRCode(border=1, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(data)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    lines: list[str] = []
    for y in range(0, len(matrix), 2):
        top = matrix[y]
        bottom = matrix[y + 1] if y + 1 < len(matrix) else [False] * len(top)
        line = "".join(
            "█" if t and b else "▀" if t else "▄" if b else " "
            for t, b in zip(top, bottom, strict=True)
        )
        lines.append(line)
    return "\n".join(lines)


def wifi_qr_payload(ssid: str, password: str, hidden: bool = False) -> str:
    def escape(value: str) -> str:
        return (
            value.replace("\\", "\\\\")
            .replace(";", "\\;")
            .replace(",", "\\,")
            .replace(":", "\\:")
            .replace('"', '\\"')
        )

    hidden_value = "H:true;" if hidden else ""
    return f"WIFI:T:WPA;S:{escape(ssid)};P:{escape(password)};{hidden_value};"


class TuiController:
    def __init__(
        self,
        config_path: Path | str,
        *,
        dry_run: bool = False,
        runtime: Runtime | None = None,
    ) -> None:
        self.config_path = Path(config_path)
        self.dry_run = dry_run
        self._runtime = runtime
        self._lock = threading.RLock()

    # -- runtime -------------------------------------------------------
    @property
    def runtime(self) -> Runtime:
        with self._lock:
            if self._runtime is None:
                self._runtime = self._build_runtime()
            return self._runtime

    def _build_runtime(self) -> Runtime:
        config: AppConfig | None = None
        if self.config_path.exists():
            try:
                config = load_config(self.config_path)
            except ConfigError:
                config = None
        if config is None:
            config = default_config()
        database: Database | None = None
        try:
            if is_root():
                database = open_database(config.db_path)
        except OSError:
            database = None
        if database is None:
            database = Database(":memory:")
            database.migrate()
        return build_runtime(
            config=config,
            config_path=self.config_path,
            db=database,
            dry_run=self.dry_run,
        )

    def _load_config(self) -> AppConfig:
        text = self.runtime.fs.read_text(self.config_path)
        raw = yaml.safe_load(text)
        return AppConfig.model_validate(raw)

    def reload(self) -> None:
        with self._lock:
            runtime = self.runtime
            if runtime.fs.exists(self.config_path):
                with contextlib.suppress(Exception):
                    runtime.config = self._load_config()
            if runtime.db.path == ":memory:" and is_root():
                try:
                    runtime.db.close()
                    runtime.db = open_database(runtime.config.db_path)
                except OSError:
                    pass

    @property
    def installed(self) -> bool:
        try:
            return bool(self.runtime.fs.exists(self.config_path))
        except Exception:
            return self.config_path.exists()

    @property
    def config(self) -> AppConfig:
        return self.runtime.config

    @property
    def privileged(self) -> bool:
        return self.runtime.privileged

    # -- discovery -----------------------------------------------------
    def registry(self) -> ServiceRegistry:
        return ServiceRegistry(self.runtime)

    def hardware(self) -> HardwareReport:
        return detect_hardware(self.runtime)

    def statuses(self) -> dict[str, ServiceStatus]:
        return self.registry().statuses()

    def infos(self) -> list[Any]:
        return self.registry().infos()

    # -- configuration -------------------------------------------------
    def save_config(self, config: AppConfig) -> ActionResult:
        if not self.privileged and not self.dry_run:
            return ActionResult.failure("saving configuration requires root")
        try:
            self.runtime.fs.atomic_write(
                self.config_path, config_yaml(config), mode=0o600
            )
        except OSError as exc:
            return ActionResult.failure(f"could not write config: {exc}")
        self.runtime.config = config
        return ActionResult.success("configuration saved")

    def save_patch(self, patch: Mapping[str, Any]) -> ActionResult:
        try:
            config = merge_config(self.runtime.config, patch)
        except ConfigError as exc:
            return ActionResult.failure(str(exc))
        return self.save_config(config)

    def apply(self) -> ApplyResult:
        if not self.privileged and not self.dry_run:
            return ApplyResult(ok=False, message="applying configuration requires root")
        return self.registry().apply(dry_run=self.dry_run)

    def toggle_module(self, name: str, enabled: bool) -> ActionResult:
        registry = self.registry()
        service = registry.get(name)
        try:
            config = merge_config(
                self.runtime.config, {service.config_key: {"enabled": enabled}}
            )
        except ConfigError as exc:
            return ActionResult.failure(str(exc))
        saved = self.save_config(config)
        if not saved.ok:
            return saved
        if not enabled:
            stopped = service.stop()
            if not stopped.ok:
                return ActionResult.failure(
                    f"{name} disabled in config but stop failed: {stopped.message}"
                )
            return ActionResult.success(f"{name} disabled")
        result = registry.apply(names=[name], dry_run=self.dry_run)
        if not result.ok:
            return ActionResult.failure(result.message)
        start = service.start()
        return ActionResult.success(f"{name} enabled") if start.ok else start

    def module_action(self, name: str, action: str) -> ActionResult:
        return self.registry().control(name, action)

    def reinstall_module(self, name: str) -> ActionResult:
        return Installer(self.runtime).reinstall_module(name)

    # -- install / update ---------------------------------------------
    def install(
        self,
        *,
        answers: Mapping[str, Any] | None = None,
        progress: Progress | None = None,
        cancel: threading.Event | None = None,
    ) -> OpResult:
        if not self.privileged and not self.dry_run:
            return OpResult(False, "install requires root (run: sudo eventhub)")
        if self.runtime.db.path == ":memory:" and self.privileged:
            try:
                self.runtime.db.close()
                self.runtime.db = open_database(self.runtime.config.db_path)
            except OSError as exc:
                return OpResult(False, f"could not open state database: {exc}")
        if answers:
            try:
                self.runtime.config = merge_config(self.runtime.config, answers)
            except ConfigError as exc:
                return OpResult(False, str(exc))
        installer = Installer(self.runtime)
        log = progress or (lambda message: None)
        try:
            result = installer.install(
                unattended=True,
                dry_run=self.dry_run,
                progress=lambda step, state, message: log(
                    f"[{state}] {step}: {message}"
                ),
                cancel=(lambda: bool(cancel and cancel.is_set())),
            )
        except EventHubError as exc:
            return OpResult(False, str(exc))
        if result.ok:
            self.reload()
        return OpResult(
            result.ok,
            result.message,
            payload={
                "result": result,
                "admin_password": result.admin_password,
                "warnings": result.warnings,
            },
        )

    def update(self, *, progress: Progress | None = None) -> OpResult:
        log = progress or (lambda message: None)
        log("checking the update remote...")
        result = Installer(self.runtime).update(check_only=False, yes=True)
        if result.ok:
            self.reload()
        return OpResult(result.ok, result.message)

    def uninstall(
        self,
        mode: str,
        *,
        confirm: str = "",
        progress: Progress | None = None,
    ) -> OpResult:
        log = progress or (lambda message: None)
        log(f"uninstalling ({mode})...")
        result = Installer(self.runtime).uninstall(
            mode=mode, yes=True, confirm=confirm
        )
        return OpResult(result.ok, result.message, payload=result.details)

    def selftest(self, *, progress: Progress | None = None) -> OpResult:
        from eventhub.system.selftest import SelfTest

        report = SelfTest(self.runtime).run()
        return OpResult(report.ok, report.summary(), payload=report)

    # -- backup --------------------------------------------------------
    def backups(self) -> list[BackupInfo]:
        return BackupManager(self.runtime).list_backups()

    def export_backup(self, *, progress: Progress | None = None) -> OpResult:
        log = progress or (lambda message: None)
        log("creating backup...")
        try:
            info = BackupManager(self.runtime).export(include_events=True)
        except (OSError, EventHubError) as exc:
            return OpResult(False, f"backup failed: {exc}")
        return OpResult(True, f"backup written to {info.path}", payload=info)

    def restore_backup(self, path: str, *, progress: Progress | None = None) -> OpResult:
        log = progress or (lambda message: None)
        log(f"restoring {path}...")
        try:
            BackupManager(self.runtime).restore(path)
            self.reload()
        except (OSError, EventHubError, ConfigError) as exc:
            return OpResult(False, f"restore failed: {exc}")
        applied = self.apply()
        if not applied.ok:
            return OpResult(False, f"restored, but apply failed: {applied.message}")
        return OpResult(True, "backup restored and applied")

    # -- logs / recovery ----------------------------------------------
    def logs(self, module: str, *, lines: int = 200) -> str:
        try:
            service = self.registry().get(module)
            if service.systemd_units:
                chunks = [
                    self.runtime.systemd.journal(unit, lines=lines)
                    for unit in service.systemd_units
                ]
                text = "\n".join(chunk for chunk in chunks if chunk)
                if text:
                    return text
        except EventHubError:
            pass
        log_file = self.runtime.config.log_dir / "eventhub.log"
        return "\n".join(tail_file(log_file, lines=lines))

    def recovery(self, action: str, *, progress: Progress | None = None) -> OpResult:
        log = progress or (lambda message: None)
        log(f"recovery: {action}...")
        try:
            from eventhub.system.recovery import RecoveryManager
        except ImportError:
            return OpResult(False, "recovery module is not available in this build")
        try:
            result = RecoveryManager(self.runtime).run(action)
        except EventHubError as exc:
            return OpResult(False, str(exc))
        return OpResult(result.ok, result.message, payload=result.details)

    # -- presentation helpers -----------------------------------------
    def dashboard_url(self) -> str:
        config = self.runtime.config
        port = config.dashboard.port
        suffix = "" if port == 80 else f":{port}"
        return f"http://{config.discovery.hostname}.local{suffix}/"

    def wifi_qr(self) -> str:
        config = self.runtime.config
        return qr_ascii(
            wifi_qr_payload(
                config.hotspot.ssid,
                config.hotspot.password.get_secret_value(),
                config.hotspot.hidden,
            )
        )
