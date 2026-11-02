"""Shared dashboard helpers: sessions, CSRF, helper calls and rendering."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from eventhub.dashboard.auth import SessionAuth, SessionUser
from eventhub.dashboard.settings import DashboardSettings
from eventhub.helper.actions import HelperResponse
from eventhub.helper.client import HelperClient, HelperError, HelperUnavailable


def settings(request: Request) -> DashboardSettings:
    return request.app.state.settings  # type: ignore[no-any-return]


def auth(request: Request) -> SessionAuth:
    return request.app.state.auth  # type: ignore[no-any-return]


def helper(request: Request) -> HelperClient:
    return request.app.state.helper  # type: ignore[no-any-return]


def templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates  # type: ignore[no-any-return]


def current_session(request: Request) -> SessionUser | None:
    cookie = request.cookies.get(settings(request).cookie_name)
    if not cookie:
        return None
    return auth(request).lookup(cookie)


def require_user(request: Request) -> SessionUser:
    session = current_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail="login required")
    return session


def require_admin(request: Request) -> SessionUser:
    session = require_user(request)
    if not session.is_admin:
        raise HTTPException(status_code=403, detail="administrator access required")
    return session


def require_csrf(request: Request, session: SessionUser, token: str | None) -> None:
    if not SessionAuth.check_csrf(session, token):
        raise HTTPException(status_code=403, detail="invalid CSRF token")


async def helper_call(
    request: Request, action: str, payload: Mapping[str, Any] | None = None
) -> HelperResponse:
    try:
        response = await helper(request).call(action, payload or {})
    except HelperUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except HelperError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not response.ok:
        raise HTTPException(status_code=400, detail=response.message or f"{action} failed")
    return response


async def helper_try(
    request: Request, action: str, payload: Mapping[str, Any] | None = None
) -> HelperResponse:
    """Like helper_call but returns an error response instead of raising."""

    try:
        return await helper_call(request, action, payload)
    except HTTPException as exc:
        return HelperResponse(False, str(exc.detail))


def render(
    request: Request, name: str, *, status_code: int | None = None, **context: Any
) -> Any:
    session = current_session(request)
    base: dict[str, Any] = {
        "request": request,
        "settings": settings(request),
        "session": session,
        "csrf": session.csrf_token if session else "",
        "path": request.url.path,
    }
    base.update(context)
    kwargs: dict[str, Any] = {}
    if status_code is not None:
        kwargs["status_code"] = status_code
    return templates(request).TemplateResponse(request, name, base, **kwargs)


def flash_redirect(
    path: str, *, ok: str | None = None, error: str | None = None
) -> RedirectResponse:
    query = ""
    if ok:
        query = "?ok=" + quote(ok)
    elif error:
        query = "?error=" + quote(error)
    return RedirectResponse(path + query, status_code=303)
