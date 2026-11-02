"""Users, groups, roles, guest accounts/links and protocol credential sync."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from eventhub.core.config import AppConfig
from eventhub.core.db import utcnow
from eventhub.core.errors import ConflictError, NotFoundError
from eventhub.core.passwords import (
    copyparty_hash,
    hash_password,
    needs_rehash,
    verify_password,
)
from eventhub.core.types import (
    ROLE_PERMISSIONS,
    ActionResult,
    Health,
    HealthResult,
    IssueLevel,
    PermissionLevel,
    RenderedFile,
    ServiceStatus,
    UserRole,
    ValidationIssue,
)
from eventhub.service_api import ServiceModule

_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{2,27}$")
_ROLES = {"admin", "editor", "shooter", "viewer"}


@dataclass(frozen=True, slots=True)
class User:
    id: int
    username: str
    display_name: str
    role: str
    enabled: bool
    must_change_password: bool
    is_guest: bool
    expires_at: str | None
    created_at: str
    updated_at: str
    last_login_at: str | None
    notes: str

    @property
    def expired(self) -> bool:
        return expires_at(self.expires_at)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
            "enabled": self.enabled,
            "must_change_password": self.must_change_password,
            "is_guest": self.is_guest,
            "expires_at": self.expires_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_login_at": self.last_login_at,
            "notes": self.notes,
            "expired": self.expired,
        }


@dataclass(frozen=True, slots=True)
class Group:
    id: int
    name: str
    description: str
    members: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "members": list(self.members),
        }


@dataclass(frozen=True, slots=True)
class GuestLink:
    id: int
    token: str
    event_id: int | None
    permission: str
    expires_at: str
    label: str


def expires_at(value: str | None) -> bool:
    if not value:
        return False
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when <= datetime.now(UTC)


def _row_to_user(row: Any) -> User:
    return User(
        id=int(row["id"]),
        username=str(row["username"]),
        display_name=str(row["display_name"]),
        role=str(row["role"]),
        enabled=bool(row["enabled"]),
        must_change_password=bool(row["must_change_password"]),
        is_guest=bool(row["is_guest"]),
        expires_at=row["expires_at"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        last_login_at=row["last_login_at"],
        notes=str(row["notes"]),
    )


def validate_username(value: str) -> str:
    username = value.strip().lower()
    if not _USERNAME_RE.match(username):
        raise ConflictError("username must be 3-28 characters: lowercase letters, digits, _ or -")
    if username in {"root", "daemon", "nobody"}:
        raise ConflictError(f"reserved username: {username}")
    return username


class UsersService(ServiceModule):
    name = "users"
    title = "Users & Access"
    description = "Users, groups, roles and protocol credential synchronisation."
    config_attr = "users"
    packages = ()

    @property
    def users_config(self) -> Any:
        return self.config.users

    # -- queries -------------------------------------------------------
    def get_user(self, user_id: int) -> User:
        row = self.runtime.db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
        if row is None:
            raise NotFoundError(f"user not found: {user_id}")
        return _row_to_user(row)

    def get_by_username(self, username: str) -> User | None:
        row = self.runtime.db.query_one(
            "SELECT * FROM users WHERE username = ?", (username.strip().lower(),)
        )
        return _row_to_user(row) if row else None

    def list_users(
        self, *, include_guests: bool = True, include_expired: bool = True
    ) -> list[User]:
        users = [
            _row_to_user(row)
            for row in self.runtime.db.query_all("SELECT * FROM users ORDER BY username")
        ]
        if not include_guests:
            users = [user for user in users if not user.is_guest]
        if not include_expired:
            users = [user for user in users if not user.expired]
        return users

    def enabled_users(self) -> list[User]:
        return [
            user
            for user in self.list_users(include_expired=False)
            if user.enabled and not user.expired
        ]

    def samba_username(self, user: User | str) -> str:
        username = user.username if isinstance(user, User) else user
        return (self.users_config.system_user_prefix + username)[:32]

    def role_permission(self, role: str) -> PermissionLevel:
        return ROLE_PERMISSIONS.get(role, PermissionLevel.NONE)

    # -- writes --------------------------------------------------------
    def _hash_copyparty(self, password: str) -> str:
        salt = self.config.fileserver_copyparty.password_salt.get_secret_value()
        return copyparty_hash(password, salt)

    def _validate_password(self, password: str) -> None:
        minimum = self.users_config.min_password_length
        if len(password) < minimum:
            raise ConflictError(f"password must be at least {minimum} characters")
        if len(password) > 128:
            raise ConflictError("password must be at most 128 characters")

    def create_user(
        self,
        username: str,
        password: str,
        *,
        role: UserRole | str = "viewer",
        display_name: str = "",
        enabled: bool = True,
        must_change_password: bool = True,
        is_guest: bool = False,
        expires_hours: int | None = None,
        notes: str = "",
    ) -> User:
        name = validate_username(username)
        if role not in _ROLES:
            raise ConflictError(f"unknown role: {role}")
        self._validate_password(password)
        if self.get_by_username(name) is not None:
            raise ConflictError(f"user already exists: {name}")
        expiry: str | None = None
        if expires_hours is not None:
            expiry = (
                (datetime.now(UTC) + timedelta(hours=expires_hours))
                .replace(microsecond=0)
                .isoformat()
            )
        now = utcnow()
        cp_hash = self._hash_copyparty(password)
        try:
            user_id = self.runtime.db.execute(
                """
                INSERT INTO users(username, display_name, password_hash, copyparty_hash,
                                  role, enabled, must_change_password, is_guest,
                                  expires_at, created_at, updated_at, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    display_name or name,
                    hash_password(password),
                    cp_hash,
                    str(role),
                    int(enabled),
                    int(must_change_password),
                    int(is_guest),
                    expiry,
                    now,
                    now,
                    notes,
                ),
            )
        except Exception as exc:
            raise ConflictError(f"could not create user: {exc}") from exc
        user = self.get_user(user_id)
        self.runtime.db.audit("user.create", target=name, detail=str(role))
        self.runtime.bus.emit("user.created", {"username": name, "role": role}, source="users")
        self._sync_create(user, password)
        return user

    def update_user(
        self,
        user_id: int,
        *,
        display_name: str | None = None,
        role: str | None = None,
        enabled: bool | None = None,
        notes: str | None = None,
        expires_at: str | None = None,
        must_change_password: bool | None = None,
    ) -> User:
        user = self.get_user(user_id)
        if role is not None and role not in _ROLES:
            raise ConflictError(f"unknown role: {role}")
        if role is not None and role != "admin" and user.role == "admin":
            self._assert_other_admin(user_id)
        if enabled is False and user.role == "admin" and user.enabled:
            self._assert_other_admin(user_id)
        changes: dict[str, Any] = {}
        if display_name is not None:
            changes["display_name"] = display_name
        if role is not None:
            changes["role"] = role
        if enabled is not None:
            changes["enabled"] = int(enabled)
        if notes is not None:
            changes["notes"] = notes
        if expires_at is not None:
            changes["expires_at"] = expires_at or None
        if must_change_password is not None:
            changes["must_change_password"] = int(must_change_password)
        if not changes:
            return user
        changes["updated_at"] = utcnow()
        columns = ", ".join(f"{key} = ?" for key in changes)
        self.runtime.db.execute(
            f"UPDATE users SET {columns} WHERE id = ?",
            (*changes.values(), user_id),
        )
        updated = self.get_user(user_id)
        self.runtime.db.audit("user.update", target=user.username, detail=json.dumps(changes))
        self.runtime.bus.emit("user.updated", {"username": user.username}, source="users")
        return updated

    def set_password(
        self, user_id: int, password: str, *, must_change: bool = False
    ) -> ActionResult:
        user = self.get_user(user_id)
        self._validate_password(password)
        self.runtime.db.execute(
            "UPDATE users SET password_hash = ?, copyparty_hash = ?, "
            "must_change_password = ?, updated_at = ? WHERE id = ?",
            (
                hash_password(password),
                self._hash_copyparty(password),
                int(must_change),
                utcnow(),
                user_id,
            ),
        )
        self.runtime.db.audit("user.password", target=user.username)
        self._sync_password(user, password)
        return ActionResult.success(f"password updated for {user.username}")

    def delete_user(self, user_id: int) -> ActionResult:
        user = self.get_user(user_id)
        if user.role == "admin" and user.enabled:
            self._assert_other_admin(user_id)
        self.runtime.db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        self.runtime.db.audit("user.delete", target=user.username)
        self.runtime.bus.emit("user.deleted", {"username": user.username}, source="users")
        self._sync_delete(user)
        return ActionResult.success(f"deleted {user.username}")

    def _assert_other_admin(self, exclude_id: int) -> None:
        count = self.runtime.db.scalar(
            "SELECT COUNT(*) FROM users WHERE role = 'admin' AND enabled = 1 AND id != ?",
            (exclude_id,),
        )
        if not count:
            raise ConflictError("cannot remove, disable or demote the last enabled admin")

    def authenticate(self, username: str, password: str, *, remote: str = "") -> User | None:
        user = self.get_by_username(username)
        if user is None or not user.enabled or user.expired:
            self.runtime.db.audit(
                "user.login_failed", actor=username, target="dashboard", remote=remote
            )
            return None
        row = self.runtime.db.query_one("SELECT password_hash FROM users WHERE id = ?", (user.id,))
        if row is None or not verify_password(str(row["password_hash"]), password):
            self.runtime.db.audit(
                "user.login_failed", actor=username, target="dashboard", remote=remote
            )
            return None
        if needs_rehash(str(row["password_hash"])):
            self.runtime.db.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                (hash_password(password), utcnow(), user.id),
            )
        self.runtime.db.execute(
            "UPDATE users SET last_login_at = ? WHERE id = ?", (utcnow(), user.id)
        )
        self.runtime.db.audit("user.login", actor=username, target="dashboard", remote=remote)
        self.runtime.bus.emit("user.login", {"username": username}, source="users")
        return self.get_user(user.id)

    def change_password(
        self, user_id: int, current_password: str, new_password: str
    ) -> ActionResult:
        user = self.get_user(user_id)
        row = self.runtime.db.query_one("SELECT password_hash FROM users WHERE id = ?", (user_id,))
        if row is None or not verify_password(str(row["password_hash"]), current_password):
            return ActionResult.failure("current password is incorrect")
        result = self.set_password(user_id, new_password, must_change=False)
        if result.ok:
            self.runtime.db.audit("user.password_change", actor=user.username)
        return result

    # -- groups --------------------------------------------------------
    def create_group(self, name: str, description: str = "") -> Group:
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{1,31}", name):
            raise ConflictError("invalid group name")
        if self.runtime.db.scalar("SELECT 1 FROM groups WHERE name = ?", (name,)):
            raise ConflictError(f"group already exists: {name}")
        group_id = self.runtime.db.execute(
            "INSERT INTO groups(name, description, created_at) VALUES (?, ?, ?)",
            (name, description, utcnow()),
        )
        return Group(group_id, name, description)

    def list_groups(self) -> list[Group]:
        groups = self.runtime.db.query_all("SELECT * FROM groups ORDER BY name")
        result = []
        for row in groups:
            members = self.runtime.db.query_all(
                "SELECT u.username FROM users u JOIN user_groups ug ON ug.user_id = u.id "
                "WHERE ug.group_id = ? ORDER BY u.username",
                (row["id"],),
            )
            result.append(
                Group(
                    int(row["id"]),
                    str(row["name"]),
                    str(row["description"]),
                    tuple(str(member["username"]) for member in members),
                )
            )
        return result

    def delete_group(self, group_id: int) -> ActionResult:
        row = self.runtime.db.query_one("SELECT name FROM groups WHERE id = ?", (group_id,))
        if row is None:
            return ActionResult.failure("group not found")
        self.runtime.db.execute("DELETE FROM groups WHERE id = ?", (group_id,))
        return ActionResult.success(f"deleted group {row['name']}")

    def add_to_group(self, user_id: int, group_id: int) -> ActionResult:
        self.get_user(user_id)
        self.runtime.db.execute(
            "INSERT OR IGNORE INTO user_groups(user_id, group_id) VALUES (?, ?)",
            (user_id, group_id),
        )
        return ActionResult.success("user added to group")

    def remove_from_group(self, user_id: int, group_id: int) -> ActionResult:
        self.runtime.db.execute(
            "DELETE FROM user_groups WHERE user_id = ? AND group_id = ?",
            (user_id, group_id),
        )
        return ActionResult.success("user removed from group")

    # -- guests --------------------------------------------------------
    def create_guest_account(
        self,
        *,
        hours: int | None = None,
        role: str = "shooter",
        display_name: str = "Guest",
    ) -> tuple[User, str]:
        if not self.users_config.allow_guest_accounts:
            raise ConflictError("guest accounts are disabled")
        hours = hours or self.users_config.guest_default_hours
        password = secrets.token_urlsafe(18)
        username = f"guest_{secrets.token_hex(4)}"
        user = self.create_user(
            username,
            password,
            role=role,
            display_name=display_name,
            must_change_password=False,
            is_guest=True,
            expires_hours=hours,
        )
        return user, password

    def create_guest_link(
        self,
        *,
        event_id: int | None = None,
        permission: str = "upload",
        hours: int | None = None,
        max_uses: int | None = None,
        label: str = "",
        created_by: int | None = None,
    ) -> GuestLink:
        if permission not in {"read", "upload", "write"}:
            raise ConflictError("guest link permission must be read, upload or write")
        hours = hours or self.users_config.guest_default_hours
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        expiry = (datetime.now(UTC) + timedelta(hours=hours)).replace(microsecond=0).isoformat()
        link_id = self.runtime.db.execute(
            """
            INSERT INTO guest_links(token_hash, event_id, permission, created_by,
                                    created_at, expires_at, max_uses, label)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token_hash,
                event_id,
                permission,
                created_by,
                utcnow(),
                expiry,
                max_uses,
                label,
            ),
        )
        self.runtime.db.audit("guest_link.create", target=str(link_id), detail=label)
        return GuestLink(link_id, token, event_id, permission, expiry, label)

    def verify_guest_link(self, token: str, *, consume: bool = True) -> GuestLink | None:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        row = self.runtime.db.query_one(
            "SELECT * FROM guest_links WHERE token_hash = ?", (token_hash,)
        )
        if row is None or row["revoked"]:
            return None
        if expires_at(str(row["expires_at"])):
            return None
        if row["max_uses"] is not None and int(row["uses"]) >= int(row["max_uses"]):
            return None
        if consume:
            self.runtime.db.execute(
                "UPDATE guest_links SET uses = uses + 1 WHERE id = ?", (row["id"],)
            )
        return GuestLink(
            int(row["id"]),
            token,
            row["event_id"],
            str(row["permission"]),
            str(row["expires_at"]),
            str(row["label"]),
        )

    def revoke_guest_links(self, event_id: int | None = None) -> int:
        if event_id is None:
            count = int(
                self.runtime.db.scalar("SELECT COUNT(*) FROM guest_links WHERE revoked = 0") or 0
            )
            self.runtime.db.execute("UPDATE guest_links SET revoked = 1")
        else:
            count = int(
                self.runtime.db.scalar(
                    "SELECT COUNT(*) FROM guest_links WHERE revoked = 0 AND event_id = ?",
                    (event_id,),
                )
                or 0
            )
            self.runtime.db.execute(
                "UPDATE guest_links SET revoked = 1 WHERE event_id = ?", (event_id,)
            )
        return count

    # -- backend sync --------------------------------------------------
    def _smb(self) -> Any | None:
        try:
            from eventhub.registry import ServiceRegistry

            registry = ServiceRegistry(self.runtime)
            if not registry.is_available("fileserver_smb"):
                return None
            service = registry.get("fileserver_smb")
            return service if service.enabled else None
        except Exception:
            return None

    def _sync_create(self, user: User, password: str) -> None:
        smb = self._smb()
        if smb is None:
            return
        try:
            result = smb.sync_user(user, password)
            if not result.ok:
                self.log.warning("SMB sync failed for %s: %s", user.username, result.message)
        except Exception:
            self.log.warning("SMB sync crashed for %s", user.username, exc_info=True)

    def _sync_password(self, user: User, password: str) -> None:
        self._sync_create(user, password)

    def _sync_delete(self, user: User) -> None:
        smb = self._smb()
        if smb is None:
            return
        try:
            smb.remove_user(user)
        except Exception:
            self.log.warning("SMB user removal failed for %s", user.username, exc_info=True)

    def sync_backends(self) -> dict[str, Any]:
        smb = self._smb()
        if smb is None:
            return {"smb": "disabled or unavailable"}
        try:
            result = smb.ensure_users(self.list_users())
            return {"smb": result.message}
        except Exception as exc:
            return {"smb": f"failed: {exc}"}

    # -- service interface --------------------------------------------
    def validate(self, config: AppConfig) -> list[ValidationIssue]:
        issues = super().validate(config)
        minimum = config.users.min_password_length
        if config.users.enabled and minimum < 12:
            issues.append(
                ValidationIssue(
                    IssueLevel.WARNING,
                    "users.min_password_length below 12 is not recommended",
                    "users.min_password_length",
                )
            )
        admins = self.runtime.db.scalar(
            "SELECT COUNT(*) FROM users WHERE role = 'admin' AND enabled = 1"
        )
        if config.users.enabled and not admins:
            issues.append(
                ValidationIssue(
                    IssueLevel.WARNING,
                    "no enabled administrator exists; dashboard login is impossible",
                    "users",
                )
            )
        return issues

    def render(self, config: AppConfig) -> list[RenderedFile]:
        return []

    def apply(self) -> ActionResult:
        if not self.config.users.enabled:
            return ActionResult.success("users module disabled")
        self.purge_expired_guests()
        sync = self.sync_backends()
        for backend, message in sync.items():
            if "failed" in message:
                self.log.warning("%s sync: %s", backend, message)
        return ActionResult.success(
            f"{len(self.list_users())} user(s); " + "; ".join(sync.values())
        )

    def purge_expired_guests(self) -> int:
        rows = self.runtime.db.query_all(
            "SELECT id, expires_at FROM users WHERE is_guest = 1 AND expires_at IS NOT NULL"
        )
        removed = 0
        for row in rows:
            if expires_at(str(row["expires_at"])):
                try:
                    self.delete_user(int(row["id"]))
                    removed += 1
                except Exception:
                    self.log.warning("could not purge expired guest %s", row["id"])
        return removed

    def health_check(self) -> HealthResult:
        if not self.config.users.enabled:
            return HealthResult(True, "users module disabled")
        admins = int(
            self.runtime.db.scalar(
                "SELECT COUNT(*) FROM users WHERE role = 'admin' AND enabled = 1"
            )
            or 0
        )
        if not admins:
            return HealthResult(False, "no enabled administrator")
        return HealthResult(
            True,
            f"{len(self.list_users())} user(s), {admins} admin(s)",
            {"admins": admins},
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


SERVICE = UsersService
