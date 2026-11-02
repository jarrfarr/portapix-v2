"""Service discovery and orchestration.

Modules register by existing as ``eventhub.<name>.service`` with a module
level ``SERVICE`` class.  Third parties can also register an entry point in
the ``eventhub.services`` group.  Adding a service therefore requires no
edits here beyond the static ordering catalogue.
"""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from eventhub.core.context import Runtime
from eventhub.core.errors import NotFoundError
from eventhub.core.orchestrator import ApplyEngine
from eventhub.core.types import (
    ActionResult,
    ApplyResult,
    ModuleInfo,
    RenderedFile,
    ServiceStatus,
    ValidationIssue,
)
from eventhub.service_api import ServiceModule

# Apply order matters: interfaces before the AP, storage before events,
# users before file servers, the helper boundary before the dashboard.
SERVICE_ORDER: tuple[str, ...] = (
    "network",
    "hotspot",
    "storage",
    "events",
    "users",
    "fileserver_smb",
    "fileserver_copyparty",
    "airdrop",
    "discovery",
    "dashboard",
    "system",
)


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    name: str
    title: str
    description: str
    config_section: str


SERVICE_CATALOG: dict[str, CatalogEntry] = {
    "network": CatalogEntry(
        "network",
        "Network & Internet",
        "Interface role mapping, uplink selection, failover and nftables NAT.",
        "network",
    ),
    "hotspot": CatalogEntry(
        "hotspot",
        "Wifi Hotspot",
        "hostapd access point and dnsmasq DHCP/DNS.",
        "hotspot",
    ),
    "storage": CatalogEntry(
        "storage",
        "Storage",
        "USB drive detection, UUID mounts, formatting and free-space monitoring.",
        "storage",
    ),
    "events": CatalogEntry(
        "events",
        "Events",
        "Event lifecycle, folder layout, active ingest target and archival.",
        "events",
    ),
    "users": CatalogEntry(
        "users",
        "Users & Access",
        "Users, groups, roles and protocol credential synchronisation.",
        "users",
    ),
    "fileserver_smb": CatalogEntry(
        "fileserver_smb",
        "SMB File Server",
        "Samba shares per event, tuned for RAW photos and video.",
        "fileserver_smb",
    ),
    "fileserver_copyparty": CatalogEntry(
        "fileserver_copyparty",
        "copyparty Web Files",
        "Resumable browser uploads, previews and a photo-friendly web UI.",
        "fileserver_copyparty",
    ),
    "airdrop": CatalogEntry(
        "airdrop",
        "AirDrop Receive",
        "omdrop-owl receiver for iPhone and Mac transfers.",
        "airdrop",
    ),
    "discovery": CatalogEntry(
        "discovery",
        "mDNS Discovery",
        "Avahi hostname and SMB/copyparty service advertisement.",
        "discovery",
    ),
    "dashboard": CatalogEntry(
        "dashboard",
        "Dashboard",
        "Mobile-friendly web administration UI and API.",
        "dashboard",
    ),
    "system": CatalogEntry(
        "system",
        "System",
        "Logs, power monitoring, recovery, updates, reboot and shutdown.",
        "system",
    ),
}

_ACTION_METHODS = {
    "start": "start",
    "stop": "stop",
    "restart": "restart",
    "status": "status",
    "health": "health_check",
    "apply": "apply",
}


class ServiceRegistry:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self._classes: dict[str, type[ServiceModule]] = {}
        self._instances: dict[str, ServiceModule] = {}
        self._entry_points_loaded = False

    # -- discovery -----------------------------------------------------
    @staticmethod
    def _module_available(name: str) -> bool:
        try:
            return importlib.util.find_spec(f"eventhub.{name}.service") is not None
        except (ImportError, ModuleNotFoundError, ValueError):
            return False

    def available_names(self) -> list[str]:
        names = [name for name in SERVICE_ORDER if self._module_available(name)]
        names.extend(self._entry_point_names())
        return names

    def _entry_point_names(self) -> list[str]:
        self._load_entry_points()
        return [name for name in self._classes if name not in SERVICE_ORDER]

    def _load_entry_points(self) -> None:
        if self._entry_points_loaded:
            return
        self._entry_points_loaded = True
        try:
            from importlib.metadata import entry_points

            found = entry_points(group="eventhub.services")
        except Exception:
            return
        for entry in found:
            try:
                loaded = entry.load()
            except Exception:
                self.runtime.logger.warning(
                    "could not load service entry point %s", entry.name, exc_info=True
                )
                continue
            if isinstance(loaded, type) and issubclass(loaded, ServiceModule):
                self._classes.setdefault(loaded.name, loaded)

    def _load(self, name: str) -> type[ServiceModule] | None:
        if name in self._classes:
            return self._classes[name]
        self._load_entry_points()
        if name in self._classes:
            return self._classes[name]
        if name not in SERVICE_ORDER:
            return None
        try:
            module = importlib.import_module(f"eventhub.{name}.service")
        except (ImportError, ModuleNotFoundError):
            return None
        service_class = getattr(module, "SERVICE", None)
        if isinstance(service_class, type) and issubclass(service_class, ServiceModule):
            self._classes[name] = service_class
            return service_class
        # Fall back to the single concrete ServiceModule in the file.
        candidates = [
            value
            for value in vars(module).values()
            if isinstance(value, type)
            and issubclass(value, ServiceModule)
            and value is not ServiceModule
            and getattr(value, "name", "") == name
        ]
        if len(candidates) == 1:
            self._classes[name] = candidates[0]
            return candidates[0]
        return None

    # -- access --------------------------------------------------------
    def names(self, *, include_unavailable: bool = False) -> list[str]:
        available = set(self.available_names())
        if include_unavailable:
            ordered = list(SERVICE_ORDER)
            ordered.extend(name for name in available if name not in ordered)
            return ordered
        return [name for name in SERVICE_ORDER if name in available]

    def is_available(self, name: str) -> bool:
        return self._load(name) is not None

    def get(self, name: str) -> ServiceModule:
        if name in self._instances:
            instance = self._instances[name]
            if instance.config is not self.runtime.config:
                instance = self._instantiate(name)
            return instance
        return self._instantiate(name)

    def _instantiate(self, name: str) -> ServiceModule:
        service_class = self._load(name)
        if service_class is None:
            raise NotFoundError(f"service module not available: {name}")
        instance = service_class(self.runtime)
        self._instances[name] = instance
        return instance

    def __contains__(self, name: str) -> bool:
        return self.is_available(name)

    # -- collections ---------------------------------------------------
    def services(self, *, include_disabled: bool = False) -> list[ServiceModule]:
        result: list[ServiceModule] = []
        for name in self.names():
            try:
                service = self.get(name)
            except Exception:
                self.runtime.logger.warning("could not construct service %s", name, exc_info=True)
                continue
            if include_disabled or service.enabled:
                result.append(service)
        return result

    def all_services(self) -> list[ServiceModule]:
        """Every available module, enabled or not (used for validation)."""

        return self.services(include_disabled=True)

    def infos(self) -> list[ModuleInfo]:
        infos: list[ModuleInfo] = []
        for name in self.names(include_unavailable=True):
            catalog = SERVICE_CATALOG.get(
                name,
                CatalogEntry(name, name.replace("_", " ").title(), "", name),
            )
            available = self.is_available(name)
            if available:
                try:
                    infos.append(self.get(name).info(available=True))
                    continue
                except Exception:
                    available = False
            infos.append(
                ModuleInfo(
                    name=catalog.name,
                    title=catalog.title,
                    description=catalog.description,
                    config_section=catalog.config_section,
                    available=available,
                )
            )
        return infos

    def statuses(self) -> dict[str, ServiceStatus]:
        result: dict[str, ServiceStatus] = {}
        for name in self.names(include_unavailable=True):
            if not self.is_available(name):
                result[name] = ServiceStatus(
                    name=name,
                    installed=False,
                    enabled=False,
                    running=False,
                    detail="module not available in this build",
                )
                continue
            try:
                result[name] = self.get(name).status()
            except Exception as exc:
                result[name] = ServiceStatus(
                    name=name,
                    installed=False,
                    enabled=False,
                    running=False,
                    detail=f"status check failed: {exc}",
                )
        return result

    def health(self) -> dict[str, object]:
        return {
            name: self.get(name).health_check()
            for name in self.names()
        }

    def control(self, name: str, action: str) -> ActionResult:
        method_name = _ACTION_METHODS.get(action)
        if method_name is None:
            valid = ", ".join(sorted(_ACTION_METHODS))
            return ActionResult.failure(f"unknown action {action!r}; expected one of {valid}")
        service = self.get(name)
        try:
            result = getattr(service, method_name)()
        except Exception as exc:
            return ActionResult.failure(f"{name}.{action} failed: {exc}")
        if isinstance(result, ActionResult):
            return result
        if isinstance(result, bool):
            return ActionResult(result, f"{name}.{action} {'ok' if result else 'failed'}")
        return ActionResult.success(f"{name}.{action} ok")

    # -- apply ---------------------------------------------------------
    def validate_all(self) -> list[ValidationIssue]:
        return ApplyEngine(self.runtime).validate(self.all_services())

    def render_all(self) -> dict[str, list[RenderedFile]]:
        engine = ApplyEngine(self.runtime)
        rendered, _ = engine.render(self.all_services())
        return rendered

    def apply(
        self,
        *,
        names: Sequence[str] | None = None,
        dry_run: bool = False,
        reload_services: bool = True,
    ) -> ApplyResult:
        services = [self.get(name) for name in names] if names else self.all_services()
        return ApplyEngine(self.runtime).apply(
            services, dry_run=dry_run, reload_services=reload_services
        )


def load_registry(runtime: Runtime) -> ServiceRegistry:
    return ServiceRegistry(runtime)


def registered_names() -> list[str]:
    """Convenience for docs/tests: names that have a discoverable module."""

    return [name for name in SERVICE_ORDER if ServiceRegistry._module_available(name)]


def dependency_order(names: Iterable[str]) -> list[str]:
    """Order an arbitrary set of names according to the static apply order."""

    wanted = set(names)
    return [name for name in SERVICE_ORDER if name in wanted]
