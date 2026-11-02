"""Dashboard sessions, login rate limiting and CSRF tokens."""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from eventhub.core.db import Database, utcnow
from eventhub.core.passwords import hash_password, needs_rehash, verify_password
from eventhub.dashboard.settings import DashboardSettings

_SALT = "eventhub-dashboard-session"


def _expired(value: str | None) -> bool:
    if not value:
        return False
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when <= datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class SessionUser:
    session_id: str
    user_id: int
    username: str
    role: str
    must_change_password: bool
    csrf_token: str
    expires_at: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class LoginLimiter:
    def __init__(self, limit: int, window: int) -> None:
        self.limit = limit
        self.window = window
        self._lock = threading.Lock()
        self._attempts: dict[str, deque[float]] = {}

    def _prune(self, key: str, now: float) -> deque[float]:
        attempts = self._attempts.setdefault(key, deque())
        while attempts and now - attempts[0] > self.window:
            attempts.popleft()
        return attempts

    def allowed(self, key: str) -> bool:
        with self._lock:
            return len(self._prune(key, time.monotonic())) < self.limit

    def failure(self, key: str) -> None:
        with self._lock:
            self._prune(key, time.monotonic()).append(time.monotonic())

    def success(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)

    def retry_after(self, key: str) -> int:
        with self._lock:
            attempts = self._prune(key, time.monotonic())
            if not attempts:
                return 0
            return max(0, int(self.window - (time.monotonic() - attempts[0])))


class SessionAuth:
    def __init__(self, settings: DashboardSettings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.serializer = URLSafeTimedSerializer(
            settings.secret_key.get_secret_value(), salt=_SALT
        )
        self.limiter = LoginLimiter(settings.login_rate_limit, settings.login_rate_window)

    # -- cookies -------------------------------------------------------
    def sign(self, session_id: str) -> str:
        return str(self.serializer.dumps(session_id))

    def unsign(self, cookie: str) -> str | None:
        try:
            return str(
                self.serializer.loads(
                    cookie, max_age=self.settings.session_hours * 3600
                )
            )
        except (SignatureExpired, BadSignature, TypeError):
            return None

    # -- login ---------------------------------------------------------
    def authenticate(
        self, username: str, password: str, *, remote: str = ""
    ) -> tuple[SessionUser | None, str]:
        key = remote or "unknown"
        if not self.limiter.allowed(key):
            return None, f"too many attempts; try again in {self.limiter.retry_after(key)}s"
        row = self.db.query_one(
            "SELECT * FROM users WHERE username = ?", (username.strip().lower(),)
        )
        if (
            row is None
            or not row["enabled"]
            or _expired(row["expires_at"])
            or not verify_password(str(row["password_hash"]), password)
        ):
            self.limiter.failure(key)
            self.db.audit(
                "dashboard.login_failed", actor=username, target="dashboard", remote=remote
            )
            return None, "invalid username or password"
        if needs_rehash(str(row["password_hash"])):
            self.db.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                (hash_password(password), utcnow(), row["id"]),
            )
        self.limiter.success(key)
        self.db.execute(
            "UPDATE users SET last_login_at = ? WHERE id = ?", (utcnow(), row["id"])
        )
        session = self.create_session(
            int(row["id"]), remote=remote, must_change=bool(row["must_change_password"])
        )
        self.db.audit(
            "dashboard.login", actor=str(row["username"]), target="dashboard", remote=remote
        )
        return session, ""

    def create_session(
        self, user_id: int, *, remote: str = "", must_change: bool = False
    ) -> SessionUser:
        session_id = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        expires = now.replace(microsecond=0) + timedelta(
            hours=self.settings.session_hours
        )
        expires_text = expires.isoformat()
        self.db.execute(
            """
            INSERT INTO sessions(id, user_id, csrf_token, created_at, expires_at,
                                 last_seen_at, ip, user_agent)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                user_id,
                csrf,
                now.replace(microsecond=0).isoformat(),
                expires_text,
                now.replace(microsecond=0).isoformat(),
                remote,
                "",
            ),
        )
        row = self.db.query_one(
            "SELECT username, role FROM users WHERE id = ?", (user_id,)
        )
        return SessionUser(
            session_id=session_id,
            user_id=user_id,
            username=str(row["username"]) if row else "",
            role=str(row["role"]) if row else "viewer",
            must_change_password=must_change,
            csrf_token=csrf,
            expires_at=expires_text,
        )

    def lookup(self, cookie: str | None) -> SessionUser | None:
        if not cookie:
            return None
        session_id = self.unsign(cookie)
        if not session_id:
            return None
        row = self.db.query_one(
            """
            SELECT s.id, s.user_id, s.csrf_token, s.expires_at,
                   u.username, u.role, u.enabled, u.expires_at AS user_expires,
                   u.must_change_password
            FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.id = ? AND s.revoked = 0
            """,
            (session_id,),
        )
        if row is None or _expired(str(row["expires_at"])):
            return None
        if not row["enabled"] or _expired(row["user_expires"]):
            return None
        now = utcnow()
        self.db.execute("UPDATE sessions SET last_seen_at = ? WHERE id = ?", (now, session_id))
        return SessionUser(
            session_id=session_id,
            user_id=int(row["user_id"]),
            username=str(row["username"]),
            role=str(row["role"]),
            must_change_password=bool(row["must_change_password"]),
            csrf_token=str(row["csrf_token"]),
            expires_at=str(row["expires_at"]),
        )

    def revoke(self, session_id: str) -> None:
        self.db.execute("UPDATE sessions SET revoked = 1 WHERE id = ?", (session_id,))

    def revoke_user_sessions(self, user_id: int) -> None:
        self.db.execute("UPDATE sessions SET revoked = 1 WHERE user_id = ?", (user_id,))

    def cleanup(self) -> None:
        self.db.execute(
            "DELETE FROM sessions WHERE expires_at < ? OR revoked = 1", (utcnow(),)
        )

    @staticmethod
    def check_csrf(session: SessionUser, token: str | None) -> bool:
        return bool(token) and hmac.compare_digest(session.csrf_token, token)
