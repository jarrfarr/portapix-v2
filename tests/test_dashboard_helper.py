from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from fastapi.testclient import TestClient

from eventhub.core.config import config_yaml
from eventhub.dashboard.app import create_app
from eventhub.dashboard.auth import LoginLimiter
from eventhub.dashboard.service import DashboardService
from eventhub.dashboard.settings import DashboardSettings
from eventhub.helper.actions import ActionDispatcher, HelperResponse
from eventhub.helper.client import HelperClient
from eventhub.helper.server import HelperServer
from eventhub.users.service import UsersService
from tests.support import make_runtime


class FakeHelper:
    def __init__(self, responses: dict[str, object] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call(self, action: str, payload: dict[str, object] | None = None) -> HelperResponse:
        self.calls.append((action, dict(payload or {})))
        data = self.responses.get(action)
        if isinstance(data, Exception):
            return HelperResponse(False, str(data))
        return HelperResponse(True, "ok", data)


def admin_app(tmp_path: Path, responses: dict[str, object] | None = None):
    runtime = make_runtime(tmp_path)
    runtime.fs.atomic_write(runtime.config_path, config_yaml(runtime.config))
    users = UsersService(runtime)
    users.create_user(
        "admin",
        "admin-password-123",
        role="admin",
        must_change_password=False,
    )
    settings = DashboardSettings.from_config(runtime.config)
    helper = FakeHelper(responses)
    app = create_app(settings, db=runtime.db, helper_client=helper)
    return app, runtime, settings, helper


def login(client: TestClient, username: str = "admin", password: str = "admin-password-123"):
    return client.post(
        "/login",
        data={"username": username, "password": password, "next": "/"},
        follow_redirects=False,
    )


def csrf_for(app, client: TestClient) -> str:
    session = app.state.auth.lookup(client.cookies.get(app.state.settings.cookie_name))
    assert session is not None
    return session.csrf_token


def test_login_required_redirect(tmp_path: Path) -> None:
    app, _runtime, _settings, _helper = admin_app(tmp_path)
    client = TestClient(app)
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_login_and_overview(tmp_path: Path) -> None:
    responses = {
        "overview": {
            "statuses": {
                "hotspot": {
                    "installed": True,
                    "enabled": True,
                    "running": True,
                    "healthy": "healthy",
                    "detail": "ok",
                    "name": "hotspot",
                    "units": [],
                }
            },
            "uplink": {"kind": "offline", "interface": "", "online": False, "detail": "none"},
            "roles": {"ap": "wlan0", "lan": "eth0"},
            "wifi_clients": [],
            "wired_clients": [],
            "storage": {"mounted": False, "mount_point": "/media/eventhub/data"},
            "system": {
                "cpu_temp_c": 45.0,
                "load": [0.1, 0.2, 0.3],
                "memory": None,
                "throttled": None,
                "ups": [],
            },
        }
    }
    app, _runtime, _settings, _helper = admin_app(tmp_path, responses)
    client = TestClient(app)
    response = login(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    page = client.get("/")
    assert page.status_code == 200
    assert "hotspot" in page.text
    assert "45.0 °C" in page.text


def test_login_rejects_bad_password(tmp_path: Path) -> None:
    app, _runtime, _settings, _helper = admin_app(tmp_path)
    client = TestClient(app)
    response = login(client, password="wrong-password")
    assert response.status_code == 200
    assert "invalid username or password" in response.text


def test_forgot_password_forces_change(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    UsersService(runtime).create_user(
        "admin", "admin-password-123", role="admin", must_change_password=True
    )
    settings = DashboardSettings.from_config(runtime.config)
    app = create_app(settings, db=runtime.db, helper_client=FakeHelper({}))
    client = TestClient(app)
    response = login(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/password"
    assert client.get("/", follow_redirects=False).status_code == 303


def test_password_change_verifies_current_password(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    UsersService(runtime).create_user(
        "admin", "admin-password-123", role="admin", must_change_password=False
    )
    settings = DashboardSettings.from_config(runtime.config)
    app = create_app(settings, db=runtime.db, helper_client=FakeHelper({}))
    client = TestClient(app)
    login(client)
    token = csrf_for(app, client)
    bad = client.post(
        "/password",
        data={
            "csrf_token": token,
            "current_password": "wrong-password",
            "new_password": "another-password-123",
            "confirm_password": "another-password-123",
        },
    )
    assert "current password is incorrect" in bad.text
    good = client.post(
        "/password",
        data={
            "csrf_token": token,
            "current_password": "admin-password-123",
            "new_password": "another-password-123",
            "confirm_password": "another-password-123",
        },
        follow_redirects=False,
    )
    assert good.status_code == 303


def test_csrf_required_for_post(tmp_path: Path) -> None:
    app, _runtime, _settings, _helper = admin_app(tmp_path)
    client = TestClient(app)
    login(client)
    response = client.post("/system/selftest", data={})
    assert response.status_code == 403


def test_users_page_and_pages_render(tmp_path: Path) -> None:
    responses = {
        "user.list": [
            {
                "id": 1,
                "username": "admin",
                "display_name": "Administrator",
                "role": "admin",
                "enabled": True,
                "must_change_password": False,
                "is_guest": False,
                "expires_at": None,
                "created_at": "now",
                "updated_at": "now",
                "last_login_at": None,
                "notes": "",
                "expired": False,
            }
        ],
        "event.list": {"active": None, "events": [], "matrix": []},
        "config.get": {
            "hotspot": {
                "ssid": "TestHub",
                "password": "********",
                "country_code": "GB",
                "band": "auto",
                "channel": 0,
                "subnet": "10.42.0.0/24",
                "dhcp_start": "10.42.0.10",
                "dhcp_end": "10.42.0.200",
                "hidden": False,
                "client_isolation": False,
            },
            "network": {
                "uplink": "auto",
                "internet_sharing": True,
                "wifi_client_interface": "auto",
            },
            "airdrop": {
                "enabled": False,
                "device_name": "EventHub",
                "receive_mode": "everyone",
                "interface": "auto",
                "target_follows_active_event": True,
            },
            "storage": {
                "drive": {"uuid": "", "fstype": "auto", "mount_point": "/media/eventhub/data"}
            },
            "fileserver_smb": {"enabled": True},
            "fileserver_copyparty": {"enabled": True},
            "hostname": "eventhub",
            "timezone": "Etc/UTC",
        },
        "hotspot.status": {
            "interface": "wlan0",
            "ssid": "TestHub",
            "clients": [],
            "status": {"detail": "ok", "healthy": "healthy"},
        },
        "network.status": {
            "roles": {"ap": "wlan0", "wan": "eth1", "lan": "eth0"},
            "uplink": {"kind": "offline", "interface": "", "online": False, "detail": "none"},
            "wired_clients": [],
        },
        "airdrop.status": {
            "interface": "wlx1",
            "target": "/events/active/ingest",
            "health": "ok",
            "status": {"healthy": "healthy"},
        },
        "storage.usage": {
            "usage": {"mounted": False, "mount_point": "/media/eventhub/data"},
            "drives": [],
        },
        "system.overview": {
            "cpu_temp_c": 40.0,
            "uptime_seconds": 3600,
            "load": [0, 0, 0],
            "throttled": None,
        },
        "backup.list": [],
    }
    app, _runtime, _settings, _helper = admin_app(tmp_path, responses)
    client = TestClient(app)
    login(client)
    for path, needle in (
        ("/users", "Accounts"),
        ("/events", "Create event"),
        ("/access", "Protocols"),
        ("/hotspot", "Connected clients"),
        ("/network", "Wifi scan"),
        ("/airdrop", "owly doctor"),
        ("/storage", "Detected drives"),
        ("/system", "Backup and restore"),
    ):
        response = client.get(path)
        assert response.status_code == 200, (path, response.text[:300])
        assert needle in response.text, path


def test_admin_only_users_page(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    UsersService(runtime).create_user(
        "viewer", "viewer-password-123", role="viewer", must_change_password=False
    )
    settings = DashboardSettings.from_config(runtime.config)
    app = create_app(settings, db=runtime.db, helper_client=FakeHelper({"user.list": []}))
    client = TestClient(app)
    assert login(client, "viewer", "viewer-password-123").status_code == 303
    response = client.get("/users", follow_redirects=False)
    assert response.status_code == 403


def test_dashboard_service_render(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    service = DashboardService(runtime)
    files = service.render(runtime.config)
    assert len(files) == 1
    assert files[0].path.name == "dashboard.json"
    assert files[0].mode == 0o640
    settings = DashboardSettings.model_validate_json(files[0].content)
    assert settings.port == runtime.config.dashboard.port


def test_login_limiter() -> None:
    limiter = LoginLimiter(limit=2, window=60)
    assert limiter.allowed("1.2.3.4")
    limiter.failure("1.2.3.4")
    limiter.failure("1.2.3.4")
    assert not limiter.allowed("1.2.3.4")
    limiter.success("1.2.3.4")
    assert limiter.allowed("1.2.3.4")


# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------


def test_dispatcher_status_and_config(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    dispatcher = ActionDispatcher(runtime)
    response = dispatcher.dispatch("status", {})
    assert response.ok
    assert "hotspot" in response.data
    config = dispatcher.dispatch("config.get", {})
    assert config.ok
    assert config.data["hotspot"]["password"] == "********"
    forbidden = dispatcher.dispatch("rm -rf /", {})
    assert not forbidden.ok
    assert "not allowed" in forbidden.message


def test_dispatcher_config_patch_saves(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    dispatcher = ActionDispatcher(runtime)
    response = dispatcher.dispatch("config.patch", {"patch": {"hotspot": {"ssid": "NewName"}}})
    assert response.ok, response.message
    assert runtime.fs.read_text(runtime.config_path).find("NewName") >= 0
    assert runtime.config.hotspot.ssid == "NewName"


def test_dispatcher_user_and_event(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    for section in (
        "network",
        "hotspot",
        "fileserver_smb",
        "fileserver_copyparty",
        "airdrop",
        "discovery",
        "dashboard",
        "helper",
        "system",
    ):
        getattr(runtime.config, section).enabled = False
    dispatcher = ActionDispatcher(runtime)
    created = dispatcher.dispatch(
        "user.create",
        {
            "username": "alice",
            "password": "alice-password-123",
            "role": "editor",
            "must_change_password": False,
        },
    )
    assert created.ok, created.message
    assert created.data["username"] == "alice"
    event = dispatcher.dispatch(
        "event.create",
        {"name": "Wedding", "event_date": "2026-05-01", "activate": True},
    )
    assert event.ok, event.message
    assert runtime.config.events.active_event == "wedding"
    listing = dispatcher.dispatch("event.list", {})
    assert listing.ok
    assert listing.data["events"][0]["slug"] == "wedding"


def test_helper_server_client_roundtrip(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    runtime.config.helper.socket_path = tmp_path / "run" / "helper.sock"
    server = HelperServer(runtime, peer_checker=lambda _sock: True)

    async def run() -> None:
        task = asyncio.create_task(server.serve_forever())
        for _ in range(100):
            if runtime.config.helper.socket_path.exists():
                break
            await asyncio.sleep(0.01)
        client = HelperClient(runtime.config.helper.socket_path, timeout=5)
        response = await client.call("config.get", {})
        assert response.ok
        assert "hotspot" in response.data
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(run())
