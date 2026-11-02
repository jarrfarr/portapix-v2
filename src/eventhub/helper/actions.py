"""Privileged helper actions.

Only this module runs as root.  It exposes a fixed set of high-level actions;
the dashboard never sends shell commands, paths to execute, or arbitrary
arguments.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eventhub.core.config import AppConfig, config_yaml, merge_config
from eventhub.core.context import Runtime
from eventhub.core.errors import EventHubError
from eventhub.core.logging import tail_file
from eventhub.core.types import ActionResult, PermissionLevel
from eventhub.installer.backup import BackupManager
from eventhub.installer.manager import Installer
from eventhub.registry import ServiceRegistry


@dataclass(frozen=True, slots=True)
class HelperResponse:
    ok: bool
    message: str = ""
    data: Any = None
    request_id: str = ""
    elapsed_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "data": self.data,
            "request_id": self.request_id,
            "elapsed_ms": self.elapsed_ms,
        }


def _ok(data: Any = None, message: str = "ok") -> HelperResponse:
    return HelperResponse(True, message, data)


def _fail(message: str, data: Any = None) -> HelperResponse:
    return HelperResponse(False, message, data)


class ActionDispatcher:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.log = runtime.logger.getChild("helper")
        self._handlers = {
            "apply": self.apply,
            "status": self.status,
            "modules.list": self.modules_list,
            "config.get": self.config_get,
            "config.patch": self.config_patch,
            "overview": self.overview,
            "service.start": self.service_start,
            "service.stop": self.service_stop,
            "service.restart": self.service_restart,
            "module.enable": self.module_enable,
            "module.disable": self.module_disable,
            "user.list": self.user_list,
            "user.create": self.user_create,
            "user.update": self.user_update,
            "user.delete": self.user_delete,
            "user.password": self.user_password,
            "user.guest_link": self.user_guest_link,
            "event.list": self.event_list,
            "event.create": self.event_create,
            "event.activate": self.event_activate,
            "event.archive": self.event_archive,
            "event.restore": self.event_restore,
            "event.access": self.event_access,
            "storage.drives": self.storage_drives,
            "storage.usage": self.storage_usage,
            "storage.mount": self.storage_mount,
            "storage.unmount": self.storage_unmount,
            "storage.format": self.storage_format,
            "network.status": self.network_status,
            "network.scan": self.network_scan,
            "network.connect": self.network_connect,
            "network.disconnect": self.network_disconnect,
            "network.test": self.network_test,
            "hotspot.status": self.hotspot_status,
            "hotspot.clients": self.hotspot_clients,
            "hotspot.kick": self.hotspot_kick,
            "airdrop.status": self.airdrop_status,
            "airdrop.doctor": self.airdrop_doctor,
            "airdrop.target": self.airdrop_target,
            "system.overview": self.system_overview,
            "system.logs": self.system_logs,
            "system.reboot": self.system_reboot,
            "system.shutdown": self.system_shutdown,
            "system.export": self.system_export,
            "system.import": self.system_import,
            "system.update": self.system_update,
            "selftest.run": self.selftest_run,
            "recovery.admin_password": self.recovery_admin_password,
            "recovery.hotspot": self.recovery_hotspot,
            "recovery.detect": self.recovery_detect,
            "backup.export": self.backup_export,
            "backup.list": self.backup_list,
            "backup.restore": self.backup_restore,
        }

    # -- plumbing ------------------------------------------------------
    def allowed_actions(self) -> set[str]:
        return set(self.runtime.config.helper.allowed_actions)

    def dispatch(
        self, action: str, payload: Mapping[str, Any] | None = None, *, remote: str = ""
    ) -> HelperResponse:
        started = time.monotonic()
        payload = dict(payload or {})
        if action not in self.allowed_actions():
            response = _fail(f"action not allowed: {action}")
        else:
            handler = self._handlers.get(action)
            if handler is None:
                response = _fail(f"unknown action: {action}")
            else:
                try:
                    response = handler(payload)
                except EventHubError as exc:
                    response = _fail(str(exc))
                except Exception as exc:
                    self.log.exception("helper action %s crashed", action)
                    response = _fail(f"{type(exc).__name__}: {exc}")
        elapsed = int((time.monotonic() - started) * 1000)
        result = HelperResponse(
            response.ok, response.message, response.data, response.request_id, elapsed
        )
        try:
            self.runtime.db.audit(
                f"helper.{action}",
                actor="helper",
                target=str(payload.get("username") or payload.get("slug") or ""),
                detail=result.message[:500],
                remote=remote,
            )
        except Exception:
            self.log.warning("could not audit helper action %s", action, exc_info=True)
        return result

    def _save_config(self, config: AppConfig) -> None:
        self.runtime.fs.atomic_write(
            self.runtime.config_path, config_yaml(config), mode=0o600
        )
        self.runtime.config = config

    def _registry(self) -> ServiceRegistry:
        return ServiceRegistry(self.runtime)

    def _apply(self, names: list[str] | None = None) -> ActionResult:
        result = self._registry().apply(names=names)
        if not result.ok:
            raise EventHubError(result.message)
        return ActionResult.success(result.message)

    def _apply_fileservers(self) -> None:
        names = [
            name
            for name in ("fileserver_copyparty", "fileserver_smb")
            if self._registry().is_available(name)
        ]
        if names:
            self._apply(names)

    def _user_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            from eventhub.users.service import UsersService

            service = UsersService(self.runtime)
            identifier = str(payload.get("user_id") or payload.get("username") or "")
            if identifier.isdigit():
                user = service.get_user(int(identifier))
            else:
                user = service.get_by_username(identifier)
                if user is None:
                    raise EventHubError(f"user not found: {identifier}")
            return user.as_dict()
        except EventHubError:
            raise
        except Exception as exc:
            raise EventHubError(f"could not read user: {exc}") from exc

    # -- status / config ----------------------------------------------
    def apply(self, payload: Mapping[str, Any]) -> HelperResponse:
        result = self._registry().apply(dry_run=bool(payload.get("dry_run")))
        return HelperResponse(
            result.ok, result.message, result.as_dict()
        )

    def status(self, payload: Mapping[str, Any]) -> HelperResponse:
        statuses = self._registry().statuses()
        return _ok({name: status.as_dict() for name, status in statuses.items()})

    def modules_list(self, payload: Mapping[str, Any]) -> HelperResponse:
        infos = self._registry().infos()
        return _ok(
            [
                {
                    "name": info.name,
                    "title": info.title,
                    "description": info.description,
                    "config_section": info.config_section,
                    "available": info.available,
                    "requires_root": info.requires_root,
                    "requires_reboot": info.requires_reboot,
                    "depends_on": list(info.depends_on),
                }
                for info in infos
            ]
        )

    def config_get(self, payload: Mapping[str, Any]) -> HelperResponse:
        return _ok(self.runtime.config.public_dict())

    def config_patch(self, payload: Mapping[str, Any]) -> HelperResponse:
        patch = payload.get("patch")
        if not isinstance(patch, Mapping):
            return _fail("patch must be a mapping")
        try:
            config = merge_config(self.runtime.config, patch)
        except EventHubError as exc:
            return _fail(str(exc))
        self._save_config(config)
        return _ok(config.public_dict(), "configuration saved")

    def overview(self, payload: Mapping[str, Any]) -> HelperResponse:
        statuses = self._registry().statuses()
        try:
            from eventhub.hotspot.service import HotspotService
            from eventhub.network.service import NetworkService
            from eventhub.storage.service import StorageService

            network = NetworkService(self.runtime)
            hotspot = HotspotService(self.runtime)
            storage = StorageService(self.runtime)
            data = {
                "statuses": {name: status.as_dict() for name, status in statuses.items()},
                "uplink": network.uplink_state().as_dict(),
                "roles": network.roles().as_dict(),
                "wifi_clients": hotspot.clients(),
                "wired_clients": network.wired_clients(),
                "storage": storage.usage(),
                "system": self._system_metrics(),
            }
        except Exception as exc:
            return _fail(f"could not build overview: {exc}")
        return _ok(data)

    def _system_metrics(self) -> dict[str, Any]:
        fs = self.runtime.fs
        metrics: dict[str, Any] = {}
        try:
            raw = fs.read_text("/sys/class/thermal/thermal_zone0/temp")
            metrics["cpu_temp_c"] = round(int(raw.strip()) / 1000.0, 1)
        except (OSError, ValueError):
            metrics["cpu_temp_c"] = None
        try:
            uptime = float(fs.read_text("/proc/uptime").split()[0])
            metrics["uptime_seconds"] = int(uptime)
        except (OSError, ValueError, IndexError):
            metrics["uptime_seconds"] = None
        try:
            parts = fs.read_text("/proc/loadavg").split()
            metrics["load"] = [float(parts[0]), float(parts[1]), float(parts[2])]
        except (OSError, ValueError, IndexError):
            metrics["load"] = None
        try:
            meminfo = {}
            for line in fs.read_text("/proc/meminfo").splitlines():
                key, value = line.split(":", 1)
                meminfo[key] = int(value.strip().split()[0]) * 1024
            total = meminfo.get("MemTotal", 0)
            available = meminfo.get("MemAvailable", 0)
            metrics["memory"] = {
                "total_bytes": total,
                "available_bytes": available,
                "used_percent": round((total - available) * 100 / total, 1)
                if total
                else None,
            }
        except (OSError, ValueError):
            metrics["memory"] = None
        if self.runtime.runner.which("vcgencmd") is not None:
            result = self.runtime.runner.run(["vcgencmd", "get_throttled"], timeout=5)
            if result.ok and "=" in result.stdout:
                try:
                    value = int(result.stdout.strip().split("=")[1], 16)
                    metrics["throttled"] = {
                        "raw": hex(value),
                        "undervoltage": bool(value & 0x1),
                        "frequency_capped": bool(value & 0x2),
                        "throttled": bool(value & 0x4),
                        "soft_temperature_limit": bool(value & 0x8),
                    }
                except ValueError:
                    metrics["throttled"] = None
            else:
                metrics["throttled"] = None
        else:
            metrics["throttled"] = None
        metrics["ups"] = self._ups_metrics()
        return metrics

    def _ups_metrics(self) -> list[dict[str, Any]]:
        fs = self.runtime.fs
        result: list[dict[str, Any]] = []
        base = Path("/sys/class/power_supply")
        try:
            names = fs.listdir(base)
        except OSError:
            return result
        for name in names:
            path = base / name
            try:
                capacity = fs.read_text(path / "capacity").strip()
            except (OSError, FileNotFoundError):
                continue
            result.append(
                {
                    "name": name,
                    "capacity_percent": int(capacity),
                    "status": self._read(fs, path / "status"),
                }
            )
        return result

    @staticmethod
    def _read(fs: Any, path: Path) -> str:
        try:
            return fs.read_text(path).strip()
        except (OSError, FileNotFoundError):
            return ""

    # -- service lifecycle --------------------------------------------
    def service_start(self, payload: Mapping[str, Any]) -> HelperResponse:
        return self._service_action(payload, "start")

    def service_stop(self, payload: Mapping[str, Any]) -> HelperResponse:
        return self._service_action(payload, "stop")

    def service_restart(self, payload: Mapping[str, Any]) -> HelperResponse:
        return self._service_action(payload, "restart")

    def _service_action(self, payload: Mapping[str, Any], action: str) -> HelperResponse:
        name = str(payload.get("name", ""))
        result = self._registry().control(name, action)
        return HelperResponse(result.ok, result.message, result.details)

    def module_enable(self, payload: Mapping[str, Any]) -> HelperResponse:
        return self._set_module(payload, True)

    def module_disable(self, payload: Mapping[str, Any]) -> HelperResponse:
        return self._set_module(payload, False)

    def _set_module(self, payload: Mapping[str, Any], enabled: bool) -> HelperResponse:
        name = str(payload.get("name", ""))
        service = self._registry().get(name)
        try:
            config = merge_config(
                self.runtime.config, {service.config_key: {"enabled": enabled}}
            )
        except EventHubError as exc:
            return _fail(str(exc))
        self._save_config(config)
        if not enabled:
            result = service.stop()
            return HelperResponse(result.ok, result.message)
        result = self._registry().apply(names=[name])
        return HelperResponse(result.ok, result.message, result.as_dict())

    # -- users ---------------------------------------------------------
    def user_list(self, payload: Mapping[str, Any]) -> HelperResponse:
        from eventhub.users.service import UsersService

        users = UsersService(self.runtime).list_users()
        return _ok([user.as_dict() for user in users])

    def user_create(self, payload: Mapping[str, Any]) -> HelperResponse:
        from eventhub.users.service import UsersService

        password = str(payload.get("password", ""))
        if not password:
            return _fail("password is required")
        service = UsersService(self.runtime)
        user = service.create_user(
            str(payload.get("username", "")),
            password,
            role=str(payload.get("role", "viewer")),
            display_name=str(payload.get("display_name", "")),
            must_change_password=bool(payload.get("must_change_password", True)),
            is_guest=bool(payload.get("is_guest", False)),
            expires_hours=int(payload["expires_hours"]) if payload.get("expires_hours") else None,
            notes=str(payload.get("notes", "")),
        )
        self._apply_fileservers()
        return _ok(user.as_dict(), f"created {user.username}")

    def user_update(self, payload: Mapping[str, Any]) -> HelperResponse:
        from eventhub.users.service import UsersService

        service = UsersService(self.runtime)
        user = _resolve_user(service, payload)
        updated = service.update_user(
            user.id,
            display_name=payload.get("display_name"),
            role=payload.get("role"),
            enabled=payload.get("enabled"),
            notes=payload.get("notes"),
            must_change_password=payload.get("must_change_password"),
        )
        self._apply_fileservers()
        return _ok(updated.as_dict(), f"updated {updated.username}")

    def user_delete(self, payload: Mapping[str, Any]) -> HelperResponse:
        from eventhub.users.service import UsersService

        service = UsersService(self.runtime)
        user = _resolve_user(service, payload)
        result = service.delete_user(user.id)
        self._apply_fileservers()
        return HelperResponse(result.ok, result.message)

    def user_password(self, payload: Mapping[str, Any]) -> HelperResponse:
        from eventhub.users.service import UsersService

        password = str(payload.get("password", ""))
        if not password:
            return _fail("password is required")
        service = UsersService(self.runtime)
        user = _resolve_user(service, payload)
        result = service.set_password(
            user.id, password, must_change=bool(payload.get("must_change", False))
        )
        self._apply_fileservers()
        return HelperResponse(result.ok, result.message)

    def user_guest_link(self, payload: Mapping[str, Any]) -> HelperResponse:
        from eventhub.users.service import UsersService

        link = UsersService(self.runtime).create_guest_link(
            event_id=int(payload["event_id"]) if payload.get("event_id") else None,
            permission=str(payload.get("permission", "upload")),
            hours=int(payload["hours"]) if payload.get("hours") else None,
            max_uses=int(payload["max_uses"]) if payload.get("max_uses") else None,
            label=str(payload.get("label", "")),
        )
        return _ok(
            {
                "token": link.token,
                "expires_at": link.expires_at,
                "permission": link.permission,
                "event_id": link.event_id,
            }
        )

    # -- events --------------------------------------------------------
    def event_list(self, payload: Mapping[str, Any]) -> HelperResponse:
        from eventhub.events.service import EventsService

        service = EventsService(self.runtime)
        events = service.list_events(include_archived=bool(payload.get("all", True)))
        return _ok(
            {
                "active": self.runtime.config.events.active_event,
                "events": [
                    {**event.as_dict(), "size_bytes": service.size_bytes(event.slug)}
                    for event in events
                ],
                "matrix": service.access_matrix(),
            }
        )

    def event_create(self, payload: Mapping[str, Any]) -> HelperResponse:
        from eventhub.events.service import EventsService

        event = EventsService(self.runtime).create_event(
            str(payload.get("name", "")),
            event_date=payload.get("event_date"),
            slug=payload.get("slug") or None,
            notes=str(payload.get("notes", "")),
            activate=bool(payload.get("activate", True)),
        )
        self._save_config(self.runtime.config)
        self._apply()
        return _ok(event.as_dict(), f"created {event.slug}")

    def event_activate(self, payload: Mapping[str, Any]) -> HelperResponse:
        from eventhub.events.service import EventsService

        event = EventsService(self.runtime).activate(str(payload.get("slug", "")))
        self._save_config(self.runtime.config)
        self._apply()
        return _ok(event.as_dict(), f"{event.slug} is active")

    def event_archive(self, payload: Mapping[str, Any]) -> HelperResponse:
        from eventhub.events.service import EventsService

        event = EventsService(self.runtime).archive(str(payload.get("slug", "")))
        self._save_config(self.runtime.config)
        self._apply()
        return _ok(event.as_dict(), f"{event.slug} archived")

    def event_restore(self, payload: Mapping[str, Any]) -> HelperResponse:
        from eventhub.events.service import EventsService

        event = EventsService(self.runtime).restore(str(payload.get("slug", "")))
        self._apply()
        return _ok(event.as_dict(), f"{event.slug} restored")

    def event_access(self, payload: Mapping[str, Any]) -> HelperResponse:
        from eventhub.events.service import EventsService

        EventsService(self.runtime).set_access(
            int(payload.get("user_id", 0)),
            int(payload.get("event_id", 0)),
            PermissionLevel(str(payload.get("permission", "none"))),
        )
        self._apply_fileservers()
        return _ok(message="access updated")

    # -- storage -------------------------------------------------------
    def storage_drives(self, payload: Mapping[str, Any]) -> HelperResponse:
        from eventhub.storage.service import StorageService

        service = StorageService(self.runtime)
        return _ok([drive.as_dict() for drive in service.detected_drives()])

    def storage_usage(self, payload: Mapping[str, Any]) -> HelperResponse:
        from eventhub.storage.service import StorageService

        service = StorageService(self.runtime)
        return _ok(
            {"usage": service.usage(), "drives": [d.as_dict() for d in service.detected_drives()]}
        )

    def storage_mount(self, payload: Mapping[str, Any]) -> HelperResponse:
        from eventhub.storage.service import StorageService

        result = StorageService(self.runtime).mount()
        return HelperResponse(result.ok, result.message)

    def storage_unmount(self, payload: Mapping[str, Any]) -> HelperResponse:
        from eventhub.storage.service import StorageService

        result = StorageService(self.runtime).unmount(force=bool(payload.get("force")))
        return HelperResponse(result.ok, result.message)

    def storage_format(self, payload: Mapping[str, Any]) -> HelperResponse:
        from eventhub.storage.service import StorageService

        result = StorageService(self.runtime).format(
            fstype=str(payload.get("fstype", "ext4")),
            confirmation=str(payload.get("confirmation", "")),
            label=str(payload.get("label", "EVENTHUB")),
        )
        return HelperResponse(result.ok, result.message)

    # -- network / hotspot --------------------------------------------
    def network_status(self, payload: Mapping[str, Any]) -> HelperResponse:
        from eventhub.network.service import NetworkService

        service = NetworkService(self.runtime)
        return _ok(
            {
                "roles": service.roles().as_dict(),
                "uplink": service.uplink_state().as_dict(),
                "wired_clients": service.wired_clients(),
            }
        )

    def network_scan(self, payload: Mapping[str, Any]) -> HelperResponse:
        from eventhub.network.service import NetworkService

        networks = NetworkService(self.runtime).scan_wifi(str(payload.get("interface", "")))
        return _ok([network.as_dict() for network in networks])

    def network_connect(self, payload: Mapping[str, Any]) -> HelperResponse:
        from eventhub.network.service import NetworkService

        ssid = str(payload.get("ssid", ""))
        password = str(payload.get("password", ""))
        result = NetworkService(self.runtime).connect_wifi(ssid, password)
        if not result.ok:
            return HelperResponse(False, result.message)
        networks = [
            {
                "ssid": item.ssid,
                "password": item.password.get_secret_value(),
                "priority": item.priority,
                "hidden": item.hidden,
            }
            for item in self.runtime.config.network.wifi_networks
            if item.ssid != ssid
        ]
        networks.append({"ssid": ssid, "password": password, "priority": 10})
        config = merge_config(
            self.runtime.config, {"network": {"wifi_networks": networks}}
        )
        self._save_config(config)
        self._apply(["network"])
        return HelperResponse(True, result.message)

    def network_disconnect(self, payload: Mapping[str, Any]) -> HelperResponse:
        from eventhub.network.service import NetworkService

        result = NetworkService(self.runtime).disconnect_wifi()
        return HelperResponse(result.ok, result.message)

    def network_test(self, payload: Mapping[str, Any]) -> HelperResponse:
        from eventhub.network.service import NetworkService

        result = NetworkService(self.runtime).test_connectivity()
        return HelperResponse(result.ok, result.message)

    def hotspot_status(self, payload: Mapping[str, Any]) -> HelperResponse:
        from eventhub.hotspot.service import HotspotService

        service = HotspotService(self.runtime)
        return _ok(
            {
                "interface": service.interface(),
                "ssid": service.hotspot.ssid,
                "clients": service.clients(),
                "status": service.status().as_dict(),
            }
        )

    def hotspot_clients(self, payload: Mapping[str, Any]) -> HelperResponse:
        from eventhub.hotspot.service import HotspotService

        return _ok(HotspotService(self.runtime).clients())

    def hotspot_kick(self, payload: Mapping[str, Any]) -> HelperResponse:
        from eventhub.hotspot.service import HotspotService

        result = HotspotService(self.runtime).kick(str(payload.get("mac", "")))
        return HelperResponse(result.ok, result.message)

    # -- airdrop -------------------------------------------------------
    def airdrop_status(self, payload: Mapping[str, Any]) -> HelperResponse:
        from eventhub.airdrop.service import AirdropService

        service = AirdropService(self.runtime)
        return _ok(
            {
                "interface": service.interface(),
                "target": str(service.target_dir()),
                "health": service.health_check().message,
                "status": service.status().as_dict(),
            }
        )

    def airdrop_doctor(self, payload: Mapping[str, Any]) -> HelperResponse:
        from eventhub.airdrop.service import AirdropService

        return _ok(AirdropService(self.runtime).doctor())

    def airdrop_target(self, payload: Mapping[str, Any]) -> HelperResponse:
        from eventhub.airdrop.service import AirdropService

        return _ok(str(AirdropService(self.runtime).target_dir()))

    # -- system --------------------------------------------------------
    def system_overview(self, payload: Mapping[str, Any]) -> HelperResponse:
        return _ok(self._system_metrics())

    def system_logs(self, payload: Mapping[str, Any]) -> HelperResponse:
        name = str(payload.get("name", ""))
        lines = int(payload.get("lines", 200))
        try:
            service = self._registry().get(name)
            if service.systemd_units:
                text = "\n".join(
                    self.runtime.systemd.journal(unit, lines=lines)
                    for unit in service.systemd_units
                )
            else:
                text = ""
        except EventHubError:
            text = ""
        if not text:
            text = "\n".join(
                tail_file(self.runtime.config.log_dir / "eventhub.log", lines=lines)
            )
        return _ok(text)

    def system_reboot(self, payload: Mapping[str, Any]) -> HelperResponse:
        if not payload.get("confirm"):
            return _fail("reboot requires confirm=true")
        result = self.runtime.runner.run(["systemctl", "reboot"], timeout=30)
        return HelperResponse(result.ok, "rebooting" if result.ok else result.stderr.strip())

    def system_shutdown(self, payload: Mapping[str, Any]) -> HelperResponse:
        if not payload.get("confirm"):
            return _fail("shutdown requires confirm=true")
        result = self.runtime.runner.run(["systemctl", "poweroff"], timeout=30)
        return HelperResponse(result.ok, "shutting down" if result.ok else result.stderr.strip())

    def system_export(self, payload: Mapping[str, Any]) -> HelperResponse:
        info = BackupManager(self.runtime).export(
            include_events=bool(payload.get("include_events", True))
        )
        return _ok(info.as_dict())

    def system_import(self, payload: Mapping[str, Any]) -> HelperResponse:
        path = str(payload.get("path", ""))
        if not path:
            return _fail("path is required")
        BackupManager(self.runtime).restore(path, actor="helper")
        self._apply()
        return _ok(message="backup restored and applied")

    def system_update(self, payload: Mapping[str, Any]) -> HelperResponse:
        result = Installer(self.runtime).update(
            check_only=bool(payload.get("check_only", False)),
            yes=bool(payload.get("yes", False)),
        )
        return HelperResponse(result.ok, result.message, result.details)

    def selftest_run(self, payload: Mapping[str, Any]) -> HelperResponse:
        try:
            from eventhub.system.selftest import SelfTest
        except ImportError:
            return _fail("self-test is not available in this build")
        report = SelfTest(self.runtime).run()
        return HelperResponse(report.ok, report.summary(), report.as_dict())

    # -- recovery ------------------------------------------------------
    def _recovery(self, action: str) -> HelperResponse:
        try:
            from eventhub.system.recovery import RecoveryManager
        except ImportError:
            return _fail("recovery is not available in this build")
        result = RecoveryManager(self.runtime).run(action)
        return HelperResponse(result.ok, result.message, result.details)

    def recovery_admin_password(self, payload: Mapping[str, Any]) -> HelperResponse:
        return self._recovery("reset-admin")

    def recovery_hotspot(self, payload: Mapping[str, Any]) -> HelperResponse:
        return self._recovery("reset-hotspot")

    def recovery_detect(self, payload: Mapping[str, Any]) -> HelperResponse:
        return self._recovery("detect-hardware")

    # -- backup --------------------------------------------------------
    def backup_export(self, payload: Mapping[str, Any]) -> HelperResponse:
        return self.system_export(payload)

    def backup_list(self, payload: Mapping[str, Any]) -> HelperResponse:
        return _ok([info.as_dict() for info in BackupManager(self.runtime).list_backups()])

    def backup_restore(self, payload: Mapping[str, Any]) -> HelperResponse:
        return self.system_import(payload)


def _resolve_user(service: Any, payload: Mapping[str, Any]) -> Any:
    identifier = str(payload.get("user_id") or payload.get("username") or "")
    if identifier.isdigit():
        return service.get_user(int(identifier))
    user = service.get_by_username(identifier)
    if user is None:
        raise EventHubError(f"user not found: {identifier}")
    return user


def dispatch(
    runtime: Runtime, action: str, payload: Mapping[str, Any] | None = None, *, remote: str = ""
) -> HelperResponse:
    return ActionDispatcher(runtime).dispatch(action, payload, remote=remote)
