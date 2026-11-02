"""FastAPI dashboard application and authentication routes."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from eventhub import __version__
from eventhub.core.context import Runtime
from eventhub.core.db import Database
from eventhub.core.logging import configure_logging
from eventhub.core.passwords import verify_password
from eventhub.core.templates import templates_dir
from eventhub.dashboard.auth import SessionAuth
from eventhub.dashboard.common import (
    current_session,
    helper_call,
    render,
    require_user,
)
from eventhub.dashboard.settings import DashboardSettings
from eventhub.helper.client import HelperClient

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "form-action 'self'; frame-ancestors 'none'"
    ),
}


def _template_dir() -> Path:
    root = templates_dir()
    if root is None:
        raise RuntimeError("EventHub templates not found")
    return root / "dashboard"


def _set_session_cookie(
    response: Any, settings: DashboardSettings, auth_store: SessionAuth, session_id: str
) -> None:
    response.set_cookie(
        settings.cookie_name,
        auth_store.sign(session_id),
        max_age=settings.session_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def _safe_next(value: str) -> str:
    if value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


def create_app(
    runtime_settings: DashboardSettings,
    *,
    db: Database | None = None,
    helper_client: HelperClient | None = None,
) -> FastAPI:
    database = db or Database(runtime_settings.db_path)
    auth_store = SessionAuth(runtime_settings, database)
    helper = helper_client or HelperClient(runtime_settings.helper_socket)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> Any:
        auth_store.cleanup()
        yield

    app = FastAPI(
        title="EventHub",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = runtime_settings
    app.state.db = database
    app.state.auth = auth_store
    app.state.helper = helper
    app.state.templates = Jinja2Templates(directory=str(_template_dir()))
    app.state.templates.env.globals["version"] = __version__

    trusted = list(
        dict.fromkeys(
            [*runtime_settings.trusted_hosts, "localhost", "127.0.0.1", "testserver"]
        )
    )

    @app.middleware("http")
    async def host_guard(request: Request, call_next: Any) -> Any:
        host = request.headers.get("host", "").split(":")[0].lower()
        is_ip = False
        try:
            ip_address(host)
            is_ip = True
        except ValueError:
            is_ip = False
        if not host or host in trusted or is_ip:
            return await call_next(request)
        return JSONResponse({"ok": False, "message": "untrusted Host header"}, status_code=400)

    static_dir = _template_dir() / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.middleware("http")
    async def security_and_password(request: Request, call_next: Any) -> Any:
        path = request.url.path
        if not path.startswith("/static") and path not in {
            "/login",
            "/password",
            "/logout",
            "/healthz",
        }:
            session = current_session(request)
            if session is not None and session.must_change_password:
                return RedirectResponse("/password", status_code=303)
        response = await call_next(request)
        for key, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        return response

    @app.exception_handler(HTTPException)
    async def http_exception(request: Request, exc: HTTPException) -> Any:
        if exc.status_code == 401:
            target = quote(request.url.path)
            return RedirectResponse(f"/login?next={target}", status_code=303)
        return render(
            request,
            "error.html",
            status_code=exc.status_code,
            page="error",
            status=exc.status_code,
            message=str(exc.detail),
        )

    # -- auth ----------------------------------------------------------
    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, next: str = "/") -> Any:
        if current_session(request) is not None:
            return RedirectResponse("/", status_code=303)
        return render(request, "login.html", page="login", next_path=next, error="")

    @app.post("/login", response_class=HTMLResponse)
    async def login_submit(
        request: Request,
        username: str = Form(""),
        password: str = Form(""),
        next: str = Form("/"),
    ) -> Any:
        remote = request.client.host if request.client else ""
        session, error = auth_store.authenticate(username, password, remote=remote)
        if session is None:
            return render(
                request,
                "login.html",
                page="login",
                next_path=next,
                error=error,
            )
        target = "/password" if session.must_change_password else _safe_next(next)
        response = RedirectResponse(target, status_code=303)
        _set_session_cookie(response, runtime_settings, auth_store, session.session_id)
        return response

    @app.post("/logout")
    async def logout(
        request: Request, csrf_token: str = Form("")
    ) -> Any:
        session = current_session(request)
        if session is not None:
            if not SessionAuth.check_csrf(session, csrf_token):
                raise HTTPException(status_code=403, detail="invalid CSRF token")
            auth_store.revoke(session.session_id)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(runtime_settings.cookie_name)
        return response

    @app.get("/password", response_class=HTMLResponse)
    async def password_page(request: Request) -> Any:
        session = require_user(request)
        return render(request, "password.html", page="password", session=session, error="")

    @app.post("/password", response_class=HTMLResponse)
    async def password_submit(
        request: Request,
        current_password: str = Form(""),
        new_password: str = Form(""),
        confirm_password: str = Form(""),
        csrf_token: str = Form(""),
    ) -> Any:
        session = require_user(request)
        if not SessionAuth.check_csrf(session, csrf_token):
            raise HTTPException(status_code=403, detail="invalid CSRF token")
        if new_password != confirm_password:
            return render(
                request,
                "password.html",
                page="password",
                session=session,
                error="new passwords do not match",
            )
        row = database.query_one(
            "SELECT password_hash FROM users WHERE id = ?", (session.user_id,)
        )
        if row is None or not verify_password(str(row["password_hash"]), current_password):
            return render(
                request,
                "password.html",
                page="password",
                session=session,
                error="current password is incorrect",
            )
        await helper_call(
            request,
            "user.password",
            {
                "user_id": session.user_id,
                "password": new_password,
                "must_change": False,
            },
        )
        return RedirectResponse("/?ok=" + quote("password updated"), status_code=303)

    # -- overview ------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def overview(request: Request) -> Any:
        require_user(request)
        error = request.query_params.get("error", "")
        try:
            response = await helper_call(request, "overview")
            data = response.data
        except HTTPException as exc:
            data = None
            error = str(exc.detail)
        return render(
            request,
            "overview.html",
            page="overview",
            data=data,
            error=error,
            ok=request.query_params.get("ok", ""),
        )

    @app.get("/api/overview")
    async def api_overview(request: Request) -> JSONResponse:
        require_user(request)
        try:
            response = await helper_call(request, "overview")
        except HTTPException as exc:
            return JSONResponse({"ok": False, "message": str(exc.detail)}, status_code=503)
        return JSONResponse({"ok": True, "data": response.data})

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"ok": True, "version": __version__})

    from eventhub.dashboard.admin import router as admin_router

    app.include_router(admin_router)
    return app


def run_dashboard(runtime: Runtime, host: str | None = None, port: int | None = None) -> None:
    """Compatibility entry point used by ``eventhub serve``."""

    dashboard_settings = DashboardSettings.from_config(runtime.config)
    if host:
        dashboard_settings.host = host
    if port:
        dashboard_settings.port = port
    database = Database(dashboard_settings.db_path)
    app = create_app(dashboard_settings, db=database)
    import uvicorn

    uvicorn.run(
        app,
        host=dashboard_settings.host,
        port=dashboard_settings.port,
        log_level="info",
        access_log=False,
    )


def main() -> None:
    path = Path(
        os.environ.get("EVENTHUB_DASHBOARD_CONFIG", "/etc/eventhub/dashboard.json")
    )
    dashboard_settings = DashboardSettings.load(path)
    configure_logging(
        level="INFO",
        log_dir=dashboard_settings.log_dir,
        filename="dashboard.log",
        console=False,
    )
    database = Database(dashboard_settings.db_path)
    app = create_app(dashboard_settings, db=database)
    import uvicorn

    uvicorn.run(
        app,
        host=dashboard_settings.host,
        port=dashboard_settings.port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
