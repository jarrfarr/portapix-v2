from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from eventhub.core.db import Database, open_database, utcnow


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    db = open_database(tmp_path / "eventhub.db")
    assert db.schema_version >= 1
    first = db.schema_version
    db.migrate()
    assert db.schema_version == first
    db.close()
    db2 = open_database(tmp_path / "eventhub.db")
    assert db2.schema_version == first
    db2.close()


def test_settings_roundtrip(tmp_path: Path) -> None:
    db = open_database(tmp_path / "eventhub.db")
    assert db.get_setting("missing") is None
    assert db.get_setting("missing", "fallback") == "fallback"
    db.set_setting("active_event", "wedding")
    assert db.get_setting("active_event") == "wedding"
    db.set_setting("active_event", "conference")
    assert db.get_setting("active_event") == "conference"
    db.delete_setting("active_event")
    assert db.get_setting("active_event") is None
    db.close()


def test_user_event_access_and_cascade(tmp_path: Path) -> None:
    db = open_database(tmp_path / "eventhub.db")
    now = utcnow()
    user_id = db.execute(
        "INSERT INTO users(username, password_hash, role, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?)",
        ("alice", "hash", "editor", now, now),
    )
    event_id = db.execute(
        "INSERT INTO events(slug, name, event_date, path, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("wedding", "Wedding", "2026-05-01", "/events/wedding", now, now),
    )
    db.execute(
        "INSERT INTO access(user_id, event_id, permission) VALUES (?, ?, ?)",
        (user_id, event_id, "write"),
    )
    assert db.scalar("SELECT COUNT(*) FROM access") == 1
    assert db.scalar("SELECT permission FROM access WHERE user_id=?", (user_id,)) == "write"
    db.execute("DELETE FROM users WHERE id=?", (user_id,))
    assert db.scalar("SELECT COUNT(*) FROM access") == 0
    db.close()


def test_foreign_keys_are_enforced(tmp_path: Path) -> None:
    db = open_database(tmp_path / "eventhub.db")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO access(user_id, event_id, permission) VALUES (999, 999, 'read')"
        )
    db.close()


def test_audit_log(tmp_path: Path) -> None:
    db = open_database(tmp_path / "eventhub.db")
    db.audit("login", actor="alice", target="dashboard", remote="10.42.0.5")
    rows = db.recent_audit()
    assert rows[0]["action"] == "login"
    assert rows[0]["actor"] == "alice"
    db.close()


def test_service_state(tmp_path: Path) -> None:
    db = open_database(tmp_path / "eventhub.db")
    db.record_service_state("hotspot", installed=True, enabled=True)
    row = db.service_state("hotspot")
    assert row is not None
    assert row["installed"] == 1
    assert row["enabled"] == 1
    db.record_service_state("hotspot", installed=True, enabled=False)
    row = db.service_state("hotspot")
    assert row is not None
    assert row["enabled"] == 0
    db.close()


def test_in_memory_database() -> None:
    db = Database(":memory:")
    db.migrate()
    db.set_setting("key", "value")
    assert db.get_setting("key") == "value"
    db.close()
