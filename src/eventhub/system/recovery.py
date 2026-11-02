"""Recovery mode: known-good hotspot, admin reset and hardware re-detection."""

from __future__ import annotations

import json
from pathlib import Path

from eventhub.core.config import (
    AppConfig,
    ConfigError,
    config_yaml,
    default_config,
    generate_wifi_password,
    load_config,
    merge_config,
)
from eventhub.core.context import Runtime
from eventhub.core.db import utcnow
from eventhub.core.passwords import copyparty_hash, hash_password
from eventhub.core.types import ActionResult
from eventhub.network.hardware import detect_hardware
from eventhub.registry import ServiceRegistry

RECOVERY_SSID = "EventHub-Recovery"


class RecoveryManager:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.log = runtime.logger.getChild("recovery")

    # -- helpers -------------------------------------------------------
    def _save(self, config: AppConfig) -> None:
        self.runtime.fs.atomic_write(
            self.runtime.config_path, config_yaml(config), mode=0o600
        )
        self.runtime.config = config

    def _write_secret(self, path: Path, content: str) -> None:
        self.runtime.fs.atomic_write(path, content, mode=0o600)

    def _apply(self, names: list[str]) -> ActionResult:
        result = ServiceRegistry(self.runtime).apply(names=names)
        return ActionResult(result.ok, result.message, result.as_dict())

    # -- actions -------------------------------------------------------
    def run(self, action: str) -> ActionResult:
        if action == "status":
            return self.status()
        if action in {"reset-admin", "admin_password"}:
            return self.reset_admin()
        if action in {"reset-hotspot", "hotspot"}:
            return self.reset_hotspot()
        if action in {"detect-hardware", "detect"}:
            return self.detect_hardware()
        if action == "boot-check":
            return self.boot_check()
        return ActionResult.failure(f"unknown recovery action: {action}")

    def status(self) -> ActionResult:
        config_valid = True
        error = ""
        try:
            load_config(self.runtime.config_path)
        except ConfigError as exc:
            config_valid = False
            error = str(exc)
        try:
            report = detect_hardware(self.runtime).as_dict()
        except Exception as exc:
            report = {"error": str(exc)}
        return ActionResult(
            True,
            "recovery status",
            details={
                "config_valid": config_valid,
                "config_error": error,
                "hardware": report,
                "config_path": str(self.runtime.config_path),
            },
        )

    def reset_admin(self) -> ActionResult:
        from eventhub.users.service import UsersService

        users = UsersService(self.runtime)
        password = generate_wifi_password()
        password_hash = hash_password(password)
        salt = self.runtime.config.fileserver_copyparty.password_salt.get_secret_value()
        cp_hash = copyparty_hash(password, salt)
        row = self.runtime.db.query_one(
            "SELECT id, username FROM users WHERE role = 'admin' ORDER BY id LIMIT 1"
        )
        username = "admin"
        if row is None:
            user = users.create_user(
                username,
                password,
                role="admin",
                display_name="Recovery Administrator",
                must_change_password=True,
            )
        else:
            username = str(row["username"])
            self.runtime.db.execute(
                "UPDATE users SET password_hash = ?, copyparty_hash = ?, "
                "must_change_password = 1, enabled = 1, updated_at = ? WHERE id = ?",
                (password_hash, cp_hash, utcnow(), row["id"]),
            )
            user = users.get_user(int(row["id"]))
        try:
            smb = ServiceRegistry(self.runtime).get("fileserver_smb")
            smb.sync_user(user, password)
        except Exception:
            self.log.warning("could not sync recovery admin to SMB", exc_info=True)
        path = self.runtime.config.dashboard.first_boot_password_file
        self._write_secret(
            path,
            "EventHub recovery administrator\n"
            f"Username: {username}\nPassword: {password}\n"
            "Change this password after logging in.\n",
        )
        self.runtime.db.audit("recovery.admin_password", target=username)
        return ActionResult(
            True,
            f"admin password reset for {username}",
            details={"username": username, "password": password, "file": str(path)},
        )

    def reset_hotspot(self) -> ActionResult:
        password = generate_wifi_password()
        try:
            config = merge_config(
                self.runtime.config,
                {
                    "hotspot": {
                        "enabled": True,
                        "ssid": RECOVERY_SSID,
                        "password": password,
                        "hidden": False,
                        "band": "2.4",
                        "channel": 6,
                        "auth_mode": "wpa2",
                        "subnet": "10.42.0.0/24",
                        "dhcp_start": "10.42.0.10",
                        "dhcp_end": "10.42.0.200",
                    },
                    "network": {"uplink": "auto", "internet_sharing": True},
                },
            )
        except ConfigError as exc:
            return ActionResult.failure(str(exc))
        self._save(config)
        path = Path("/var/lib/eventhub/recovery-hotspot.txt")
        self._write_secret(
            path,
            f"EventHub recovery hotspot\nSSID: {RECOVERY_SSID}\n"
            f"Password: {password}\nAddress: http://{config.hotspot.server_ip}/\n",
        )
        self.runtime.db.audit("recovery.hotspot", target=RECOVERY_SSID)
        applied = self._apply(["network", "hotspot", "discovery"])
        return ActionResult(
            applied.ok,
            f"hotspot reset to {RECOVERY_SSID}"
            + ("" if applied.ok else f" (apply failed: {applied.message})"),
            details={
                "ssid": RECOVERY_SSID,
                "password": password,
                "file": str(path),
                "apply": applied.message,
            },
        )

    def detect_hardware(self) -> ActionResult:
        try:
            report = detect_hardware(self.runtime)
        except Exception as exc:
            return ActionResult.failure(f"hardware detection failed: {exc}")
        return ActionResult(
            True,
            f"detected {report.model}",
            details=report.as_dict(),
        )

    def boot_check(self) -> ActionResult:
        """Validate config at boot; replace it with a recovery config if broken."""

        try:
            load_config(self.runtime.config_path)
            return ActionResult.success("configuration is valid")
        except ConfigError as exc:
            self.log.error("configuration invalid at boot: %s", exc)
        password = generate_wifi_password()
        try:
            config = default_config()
            config = merge_config(
                config,
                {
                    "hotspot": {
                        "enabled": True,
                        "ssid": RECOVERY_SSID,
                        "password": password,
                        "band": "2.4",
                        "channel": 6,
                        "auth_mode": "wpa2",
                    }
                },
            )
        except ConfigError as exc:
            return ActionResult.failure(f"could not build recovery config: {exc}")
        self._save(config)
        path = Path("/var/lib/eventhub/recovery-hotspot.txt")
        self._write_secret(
            path,
            f"EventHub recovery hotspot\nSSID: {RECOVERY_SSID}\n"
            f"Password: {password}\nAddress: http://{config.hotspot.server_ip}/\n",
        )
        self.runtime.db.audit("recovery.boot", detail="config replaced with recovery defaults")
        result = self._apply(["network", "hotspot", "discovery"])
        return ActionResult(
            result.ok,
            f"invalid config replaced; recovery hotspot {RECOVERY_SSID}",
            details={
                "ssid": RECOVERY_SSID,
                "password": password,
                "file": str(path),
                "apply": result.message,
            },
        )

    def export_recovery_info(self) -> str:
        return json.dumps(
            {
                "ssid": RECOVERY_SSID,
                "password_file": "/var/lib/eventhub/recovery-hotspot.txt",
                "admin_file": str(self.runtime.config.dashboard.first_boot_password_file),
            },
            indent=2,
        )
