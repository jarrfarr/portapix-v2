"""Event lifecycle, folder layout, active ingest target and archival."""

from __future__ import annotations

import contextlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from eventhub.core.config import AppConfig
from eventhub.core.db import utcnow
from eventhub.core.errors import ConflictError, NotFoundError
from eventhub.core.mounts import is_mounted
from eventhub.core.types import (
    ROLE_PERMISSIONS,
    ActionResult,
    Health,
    HealthResult,
    IssueLevel,
    PermissionLevel,
    RenderedFile,
    ServiceStatus,
    ValidationIssue,
)
from eventhub.service_api import ServiceModule

_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_USER_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")


def slugify(value: str, max_length: int = 48) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    slug = slug[:max_length].strip("-")
    return slug or "event"


@dataclass(frozen=True, slots=True)
class Event:
    id: int
    slug: str
    name: str
    event_date: str
    status: str
    path: Path
    created_at: str = ""
    updated_at: str = ""
    archived_at: str | None = None
    notes: str = ""
    metadata: dict[str, Any] | None = None

    @property
    def archived(self) -> bool:
        return self.status == "archived"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "slug": self.slug,
            "name": self.name,
            "event_date": self.event_date,
            "status": self.status,
            "path": str(self.path),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "archived_at": self.archived_at,
            "notes": self.notes,
            "metadata": self.metadata or {},
        }


def _row_to_event(row: Any) -> Event:
    try:
        metadata = json.loads(row["metadata"] or "{}")
    except ValueError:
        metadata = {}
    return Event(
        id=int(row["id"]),
        slug=str(row["slug"]),
        name=str(row["name"]),
        event_date=str(row["event_date"]),
        status=str(row["status"]),
        path=Path(str(row["path"])),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        archived_at=row["archived_at"],
        notes=str(row["notes"]),
        metadata=metadata if isinstance(metadata, dict) else {},
    )


class EventsService(ServiceModule):
    name = "events"
    title = "Events"
    description = "Event lifecycle, folder layout, active ingest target and archival."
    config_attr = "events"
    packages = ()

    @property
    def events_config(self) -> Any:
        return self.config.events

    # -- layout --------------------------------------------------------
    def events_root(self, config: AppConfig | None = None) -> Path:
        return (config or self.config).events_root

    def event_dir(self, slug: str, config: AppConfig | None = None) -> Path:
        return self.events_root(config) / slug

    def layout_paths(self, event: Event, config: AppConfig | None = None) -> dict[str, Path]:
        cfg = (config or self.config).events
        root = self.event_dir(event.slug, config)
        paths: dict[str, Path] = {}
        for entry in cfg.layout:
            rendered = entry.replace("{user}", cfg.default_ingest_user)
            paths[entry] = root / rendered
        return paths

    def _ensure_layout(self, event: Event, config: AppConfig | None = None) -> None:
        cfg = (config or self.config).events
        root = self.event_dir(event.slug, config)
        self.runtime.fs.mkdir(root, mode=0o770)
        paths = self.layout_paths(event, config)
        for path in paths.values():
            self.runtime.fs.mkdir(path, mode=0o770)
        if cfg.keep_metadata:
            metadata_dir = root / "_eventhub"
            self.runtime.fs.mkdir(metadata_dir, mode=0o770)
            payload = event.as_dict()
            payload["layout"] = {key: str(value) for key, value in paths.items()}
            self.runtime.fs.atomic_write(
                metadata_dir / "metadata.json",
                json.dumps(payload, indent=2) + "\n",
                mode=0o640,
            )
        with contextlib.suppress(KeyError, OSError):
            self.runtime.fs.chown(
                root,
                self.config.install.service_user,
                self.config.install.service_group,
            )

    # -- queries -------------------------------------------------------
    def get_event(self, slug: str) -> Event:
        row = self.runtime.db.query_one("SELECT * FROM events WHERE slug = ?", (slug,))
        if row is None:
            raise NotFoundError(f"event not found: {slug}")
        return _row_to_event(row)

    def list_events(self, *, include_archived: bool = True) -> list[Event]:
        sql = "SELECT * FROM events"
        if not include_archived:
            sql += " WHERE status != 'archived'"
        sql += " ORDER BY event_date DESC, id DESC"
        return [_row_to_event(row) for row in self.runtime.db.query_all(sql)]

    def active_event(self) -> Event | None:
        slug = self.config.events.active_event
        if not slug:
            return None
        try:
            return self.get_event(slug)
        except NotFoundError:
            return None

    def unique_slug(self, name: str) -> str:
        base = slugify(name, self.config.events.slug_max_length)
        slug = base
        counter = 2
        while self.runtime.db.scalar("SELECT 1 FROM events WHERE slug = ?", (slug,)):
            slug = f"{base[: self.config.events.slug_max_length - 3]}-{counter}"
            counter += 1
        return slug

    # -- lifecycle -----------------------------------------------------
    def _storage_ready(self, config: AppConfig | None = None) -> bool:
        cfg = (config or self.config).storage
        if not cfg.enabled or not cfg.drive.uuid:
            return True
        return is_mounted(self.runtime.fs, cfg.drive.mount_point)

    def create_event(
        self,
        name: str,
        *,
        event_date: str | date | None = None,
        slug: str | None = None,
        notes: str = "",
        activate: bool | None = None,
    ) -> Event:
        if not self.config.events.enabled:
            raise ConflictError("events module is disabled")
        if not self._storage_ready():
            raise ConflictError(
                "the event storage drive is not mounted; attach it or change the "
                "storage config before creating events"
            )
        clean_name = name.strip()
        if not clean_name:
            raise ConflictError("event name must not be empty")
        if event_date is None:
            when = self.runtime.clock.now().date()
        elif isinstance(event_date, date):
            when = event_date
        else:
            try:
                when = date.fromisoformat(event_date)
            except ValueError as exc:
                raise ConflictError(f"invalid event date: {event_date}") from exc
        chosen = slug or self.unique_slug(clean_name)
        if not _SLUG_RE.match(chosen):
            raise ConflictError(f"invalid event slug: {chosen!r}")
        if self.runtime.db.scalar("SELECT 1 FROM events WHERE slug = ?", (chosen,)):
            raise ConflictError(f"event already exists: {chosen}")
        path = self.event_dir(chosen)
        now = utcnow()
        metadata = {"owner": self.config.install.service_user, "created_by": "eventhub"}
        try:
            event_id = self.runtime.db.execute(
                """
                INSERT INTO events(slug, name, event_date, status, path,
                                   created_at, updated_at, notes, metadata)
                VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?)
                """,
                (
                    chosen,
                    clean_name,
                    when.isoformat(),
                    str(path),
                    now,
                    now,
                    notes,
                    json.dumps(metadata),
                ),
            )
        except Exception as exc:
            raise ConflictError(f"could not create event: {exc}") from exc
        event = Event(
            id=event_id,
            slug=chosen,
            name=clean_name,
            event_date=when.isoformat(),
            status="open",
            path=path,
            created_at=now,
            updated_at=now,
            notes=notes,
            metadata=metadata,
        )
        self._ensure_layout(event)
        should_activate = (
            activate
            if activate is not None
            else self.config.events.auto_activate_new_event
            and self.config.events.active_event is None
        )
        if should_activate:
            event = self.activate(chosen)
        self.runtime.db.audit("event.create", target=chosen, detail=clean_name)
        self.runtime.bus.emit(
            "event.created", {"slug": chosen, "name": clean_name}, source="events"
        )
        return event

    def activate(self, slug: str, *, force: bool = False) -> Event:
        event = self.get_event(slug)
        if event.archived and not force:
            raise ConflictError(f"event {slug} is archived; restore it before activating")
        now = utcnow()
        with self.runtime.db.transaction():
            self.runtime.db.execute(
                "UPDATE events SET status = 'open', updated_at = ? WHERE status = 'active'",
                (now,),
            )
            self.runtime.db.execute(
                "UPDATE events SET status = 'active', updated_at = ? WHERE id = ?",
                (now, event.id),
            )
        self.config.events.active_event = slug
        self._ensure_layout(event)
        self.runtime.db.audit("event.activate", target=slug)
        self.runtime.bus.emit("event.activated", {"slug": slug}, source="events")
        return self.get_event(slug)

    def archive(self, slug: str) -> Event:
        event = self.get_event(slug)
        now = utcnow()
        self.runtime.db.execute(
            "UPDATE events SET status = 'archived', archived_at = ?, updated_at = ? WHERE id = ?",
            (now, now, event.id),
        )
        if self.config.events.active_event == slug:
            self.config.events.active_event = None
        marker = self.event_dir(slug) / "_eventhub" / "ARCHIVED"
        with contextlib.suppress(OSError):
            self.runtime.fs.atomic_write(
                marker,
                f"Archived at {now}\nThis event is read-only.\n",
                mode=0o440,
            )
        self.runtime.db.audit("event.archive", target=slug)
        self.runtime.bus.emit("event.archived", {"slug": slug}, source="events")
        return self.get_event(slug)

    def restore(self, slug: str) -> Event:
        event = self.get_event(slug)
        now = utcnow()
        self.runtime.db.execute(
            "UPDATE events SET status = 'open', archived_at = NULL, updated_at = ? WHERE id = ?",
            (now, event.id),
        )
        marker = self.event_dir(slug) / "_eventhub" / "ARCHIVED"
        self.runtime.fs.remove(marker, missing_ok=True)
        self.runtime.db.audit("event.restore", target=slug)
        return self.get_event(slug)

    # -- ingest / size -------------------------------------------------
    def ingest_target(self, username: str | None = None) -> Path:
        event = self.active_event()
        if event is None:
            raise ConflictError("no active event; create or activate one first")
        if event.archived:
            raise ConflictError(f"active event {event.slug} is archived")
        cfg = self.config.events
        user = username or cfg.default_ingest_user
        if not _USER_RE.match(user):
            user = "anonymous"
        target = self.event_dir(event.slug) / "ingest" / user
        self.runtime.fs.mkdir(target, mode=0o770)
        return target

    def size_bytes(self, slug: str) -> int:
        event = self.get_event(slug)
        try:
            return self.runtime.fs.size_bytes(event.path)
        except OSError:
            return 0

    def total_bytes(self) -> int:
        try:
            return self.runtime.fs.size_bytes(self.events_root())
        except OSError:
            return 0

    # -- access --------------------------------------------------------
    def permission_for(self, user_id: int, event_id: int) -> PermissionLevel:
        row = self.runtime.db.query_one(
            "SELECT permission FROM access WHERE user_id = ? AND event_id = ?",
            (user_id, event_id),
        )
        if row is not None:
            try:
                return PermissionLevel(str(row["permission"]))
            except ValueError:
                return PermissionLevel.NONE
        role = self.runtime.db.scalar("SELECT role FROM users WHERE id = ?", (user_id,))
        return ROLE_PERMISSIONS.get(str(role), PermissionLevel.NONE)

    def set_access(
        self, user_id: int, event_id: int, permission: PermissionLevel | str
    ) -> None:
        level = PermissionLevel(str(permission))
        if level is PermissionLevel.NONE:
            self.runtime.db.execute(
                "DELETE FROM access WHERE user_id = ? AND event_id = ?",
                (user_id, event_id),
            )
        else:
            self.runtime.db.execute(
                """
                INSERT INTO access(user_id, event_id, permission) VALUES (?, ?, ?)
                ON CONFLICT(user_id, event_id) DO UPDATE SET permission = excluded.permission
                """,
                (user_id, event_id, level.value),
            )
        self.runtime.db.audit(
            "event.access", target=str(event_id), detail=f"user={user_id} {level.value}"
        )

    def access_matrix(self) -> list[dict[str, Any]]:
        users = self.runtime.db.query_all(
            "SELECT id, username, role FROM users ORDER BY username"
        )
        events = self.runtime.db.query_all("SELECT id, slug FROM events ORDER BY slug")
        matrix = []
        for user in users:
            row: dict[str, Any] = {
                "user_id": int(user["id"]),
                "username": str(user["username"]),
                "role": str(user["role"]),
                "events": {},
            }
            for event in events:
                row["events"][str(event["slug"])] = self.permission_for(
                    int(user["id"]), int(event["id"])
                ).value
            matrix.append(row)
        return matrix

    # -- service interface --------------------------------------------
    def validate(self, config: AppConfig) -> list[ValidationIssue]:
        issues = super().validate(config)
        if not config.events.enabled:
            return issues
        active = config.events.active_event
        if active:
            row = self.runtime.db.query_one("SELECT status FROM events WHERE slug = ?", (active,))
            if row is None:
                issues.append(
                    ValidationIssue(
                        IssueLevel.ERROR,
                        f"active event {active!r} does not exist in the database",
                        "events.active_event",
                    )
                )
            elif str(row["status"]) == "archived":
                issues.append(
                    ValidationIssue(
                        IssueLevel.ERROR,
                        f"active event {active!r} is archived",
                        "events.active_event",
                    )
                )
        if (
            config.storage.enabled
            and config.storage.drive.uuid
            and not is_mounted(self.runtime.fs, config.storage.drive.mount_point)
        ):
            issues.append(
                    ValidationIssue(
                        IssueLevel.WARNING,
                        "the event storage drive is not mounted; events will be created "
                        "on the mount point when it appears",
                        "storage.drive",
                    )
                )
        return issues

    def render(self, config: AppConfig) -> list[RenderedFile]:
        return []

    def apply(self) -> ActionResult:
        cfg = self.events_config
        if not cfg.enabled:
            return ActionResult.success("events module disabled")
        if not self._storage_ready():
            if self.config.storage.required:
                return ActionResult.failure("required storage drive is not mounted")
            return ActionResult.success(
                "event storage drive is not attached; skipping layout"
            )
        self.runtime.fs.mkdir(self.events_root(), mode=0o770)
        events = self.list_events()
        for event in events:
            self._ensure_layout(event)
        active = self.active_event()
        if cfg.active_event and active is None:
            return ActionResult.failure(
                f"active event {cfg.active_event!r} is missing from the database"
            )
        if active is not None and active.archived:
            return ActionResult.failure(
                f"active event {active.slug!r} is archived"
            )
        return ActionResult.success(f"{len(events)} event(s) ready")

    def health_check(self) -> HealthResult:
        if not self.config.events.enabled:
            return HealthResult(True, "events module disabled")
        root = self.events_root()
        if not self.runtime.fs.exists(root):
            return HealthResult(False, f"events root does not exist: {root}")
        active = self.active_event()
        if self.config.events.active_event and active is None:
            return HealthResult(
                False,
                f"active event {self.config.events.active_event!r} is missing",
            )
        if active is not None and not self.runtime.fs.exists(active.path):
            return HealthResult(False, f"event folder is missing: {active.path}")
        return HealthResult(
            True,
            f"{len(self.list_events())} event(s)"
            + (f"; active: {active.slug}" if active else "; no active event"),
            {"active": active.slug if active else None},
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


SERVICE = EventsService
