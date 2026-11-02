"""SQLite persistence layer.

The database is intentionally small: users, groups, events, access rules,
sessions, guest links, settings, service state and an audit log.  Modules use
the generic query helpers; core owns migrations and lifecycle.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from eventhub.core.errors import ConfigError
from eventhub.core.types import PermissionLevel, UserRole

SCHEMA_VERSION = 1

_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            display_name TEXT NOT NULL DEFAULT '',
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('admin','editor','shooter','viewer')),
            enabled INTEGER NOT NULL DEFAULT 1,
            must_change_password INTEGER NOT NULL DEFAULT 0,
            is_guest INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_login_at TEXT,
            notes TEXT NOT NULL DEFAULT ''
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            description TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS user_groups (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
            PRIMARY KEY (user_id, group_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL UNIQUE COLLATE NOCASE,
            name TEXT NOT NULL,
            event_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','active','archived')),
            path TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            archived_at TEXT,
            notes TEXT NOT NULL DEFAULT '',
            metadata TEXT NOT NULL DEFAULT '{}'
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS access (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            permission TEXT NOT NULL
                CHECK (permission IN ('none','read','upload','write','admin')),
            PRIMARY KEY (user_id, event_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            csrf_token TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            ip TEXT NOT NULL DEFAULT '',
            user_agent TEXT NOT NULL DEFAULT '',
            revoked INTEGER NOT NULL DEFAULT 0
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS guest_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_hash TEXT NOT NULL UNIQUE,
            event_id INTEGER REFERENCES events(id) ON DELETE CASCADE,
            permission TEXT NOT NULL
                CHECK (permission IN ('read','upload','write')),
            created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            max_uses INTEGER,
            uses INTEGER NOT NULL DEFAULT 0,
            revoked INTEGER NOT NULL DEFAULT 0,
            label TEXT NOT NULL DEFAULT ''
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            actor TEXT NOT NULL DEFAULT 'system',
            action TEXT NOT NULL,
            target TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '',
            remote TEXT NOT NULL DEFAULT ''
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS service_state (
            name TEXT PRIMARY KEY,
            installed INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            meta TEXT NOT NULL DEFAULT '{}'
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_access_event ON access(event_id)",
        "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts)",
    ),
}


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def utc_after(seconds: float) -> str:
    from datetime import timedelta

    return (datetime.now(UTC) + timedelta(seconds=seconds)).replace(microsecond=0).isoformat()


class Database:
    """Thread-safe wrapper around a single SQLite connection."""

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._depth = 0
        if self.path != ":memory:":
            target = Path(self.path)
            target.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA busy_timeout = 30000")

    # -- lifecycle -----------------------------------------------------
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- schema --------------------------------------------------------
    @property
    def schema_version(self) -> int:
        row = self._conn.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row else 0

    def migrate(self) -> int:
        with self._lock, self.transaction():
            version = self.schema_version
            for target in sorted(_MIGRATIONS):
                if target <= version:
                    continue
                for statement in _MIGRATIONS[target]:
                    self._conn.execute(statement)
                self._conn.execute(f"PRAGMA user_version = {target}")
                version = target
        return version

    # -- generic access ------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._depth += 1
            try:
                if self._depth == 1:
                    self._conn.execute("BEGIN IMMEDIATE")
                yield self._conn
            except BaseException:
                self._depth -= 1
                if self._depth == 0:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if self._depth == 0:
                    self._conn.execute("COMMIT")

    def execute(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> int:
        with self._lock, self.transaction() as conn:
            cursor = conn.execute(sql, params)
            return int(cursor.lastrowid or 0)

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        with self._lock, self.transaction() as conn:
            conn.executemany(sql, rows)

    def query_all(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()
    ) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def query_one(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()
    ) -> sqlite3.Row | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        if row is None:
            return None
        return cast(sqlite3.Row, row)

    def scalar(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()
    ) -> Any:
        row = self.query_one(sql, params)
        return row[0] if row else None

    # -- settings ------------------------------------------------------
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.query_one("SELECT value FROM settings WHERE key = ?", (key,))
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.execute(
            """
            INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                           updated_at = excluded.updated_at
            """,
            (key, value, utcnow()),
        )

    def delete_setting(self, key: str) -> None:
        self.execute("DELETE FROM settings WHERE key = ?", (key,))

    # -- audit ---------------------------------------------------------
    def audit(
        self,
        action: str,
        *,
        actor: str = "system",
        target: str = "",
        detail: str = "",
        remote: str = "",
    ) -> None:
        self.execute(
            "INSERT INTO audit_log(ts, actor, action, target, detail, remote)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (utcnow(), actor, action, target, detail[:4000], remote[:200]),
        )

    def recent_audit(self, limit: int = 100) -> list[sqlite3.Row]:
        return self.query_all(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (max(0, limit),)
        )

    # -- backup / restore ---------------------------------------------
    def backup(self, target: Path | str) -> None:
        """Write a consistent copy of the database using SQLite's backup API."""

        destination = sqlite3.connect(str(target), isolation_level=None)
        try:
            with self._lock:
                self._conn.backup(destination)
        finally:
            destination.close()

    def restore_from(self, source: Path | str) -> None:
        """Replace the live database contents from a backup file."""

        source_db = sqlite3.connect(str(source), isolation_level=None)
        try:
            with self._lock:
                source_db.backup(self._conn)
        finally:
            source_db.close()

    # -- service state -------------------------------------------------
    def record_service_state(
        self, name: str, *, installed: bool, enabled: bool, meta: str = "{}"
    ) -> None:
        self.execute(
            """
            INSERT INTO service_state(name, installed, enabled, updated_at, meta)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET installed = excluded.installed,
                                           enabled = excluded.enabled,
                                           updated_at = excluded.updated_at,
                                           meta = excluded.meta
            """,
            (name, int(installed), int(enabled), utcnow(), meta),
        )

    def service_state(self, name: str) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM service_state WHERE name = ?", (name,))


def open_database(path: Path | str) -> Database:
    try:
        db = Database(path)
    except (OSError, sqlite3.Error) as exc:
        raise ConfigError(f"could not open database {path}: {exc}") from exc
    db.migrate()
    return db


def ensure_schema(db: Database) -> int:
    return db.migrate()


def role_permission(role: str) -> PermissionLevel:
    from eventhub.core.types import ROLE_PERMISSIONS

    try:
        return ROLE_PERMISSIONS[role]
    except KeyError as exc:
        raise ValueError(f"unknown role: {role}") from exc


def valid_role(value: str) -> UserRole:
    if value not in {"admin", "editor", "shooter", "viewer"}:
        raise ValueError(f"unknown role: {value}")
    return value  # type: ignore[return-value]
