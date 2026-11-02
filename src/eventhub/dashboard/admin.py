"""Dashboard admin pages: users, events, network, storage and system."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from eventhub.dashboard.common import (
    flash_redirect,
    helper_call,
    render,
    require_admin,
    require_csrf,
)

router = APIRouter()
ROLES = ["admin", "editor", "shooter", "viewer"]
PERMISSIONS = ["none", "read", "upload", "write", "admin"]


def verify_csrf(request: Request, token: str) -> Any:
    session = require_admin(request)
    require_csrf(request, session, token)
    return session


def _error(path: str, exc: HTTPException) -> RedirectResponse:
    return flash_redirect(path, error=str(exc.detail))


async def _config(request: Request) -> dict[str, Any]:
    response = await helper_call(request, "config.get")
    return response.data if isinstance(response.data, dict) else {}


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------


@router.get("/users")
async def users_page(request: Request) -> Any:
    require_admin(request)
    response = await helper_call(request, "user.list")
    return render(
        request,
        "users.html",
        page="users",
        users=response.data or [],
        roles=ROLES,
        ok=request.query_params.get("ok", ""),
        error=request.query_params.get("error", ""),
    )


@router.post("/users")
async def users_create(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    role: str = Form("viewer"),
    display_name: str = Form(""),
    must_change: str = Form("1"),
    csrf_token: str = Form(""),
) -> Any:
    verify_csrf(request, csrf_token)
    try:
        await helper_call(
            request,
            "user.create",
            {
                "username": username,
                "password": password,
                "role": role,
                "display_name": display_name,
                "must_change_password": must_change == "1",
            },
        )
    except HTTPException as exc:
        return _error("/users", exc)
    return flash_redirect("/users", ok=f"created {username}")


@router.post("/users/{user_id}/update")
async def users_update(
    request: Request,
    user_id: int,
    role: str = Form("viewer"),
    display_name: str = Form(""),
    enabled: str = Form("1"),
    csrf_token: str = Form(""),
) -> Any:
    verify_csrf(request, csrf_token)
    try:
        await helper_call(
            request,
            "user.update",
            {
                "user_id": user_id,
                "role": role,
                "display_name": display_name,
                "enabled": enabled == "1",
            },
        )
    except HTTPException as exc:
        return _error("/users", exc)
    return flash_redirect("/users", ok="user updated")


@router.post("/users/{user_id}/password")
async def users_password(
    request: Request,
    user_id: int,
    password: str = Form(""),
    csrf_token: str = Form(""),
) -> Any:
    verify_csrf(request, csrf_token)
    try:
        await helper_call(
            request, "user.password", {"user_id": user_id, "password": password}
        )
    except HTTPException as exc:
        return _error("/users", exc)
    return flash_redirect("/users", ok="password updated")


@router.post("/users/{user_id}/delete")
async def users_delete(
    request: Request, user_id: int, csrf_token: str = Form("")
) -> Any:
    verify_csrf(request, csrf_token)
    try:
        await helper_call(request, "user.delete", {"user_id": user_id})
    except HTTPException as exc:
        return _error("/users", exc)
    return flash_redirect("/users", ok="user deleted")


@router.post("/users/guest-link")
async def users_guest_link(
    request: Request,
    event_id: str = Form(""),
    permission: str = Form("upload"),
    hours: str = Form("24"),
    max_uses: str = Form("0"),
    csrf_token: str = Form(""),
) -> Any:
    verify_csrf(request, csrf_token)
    try:
        response = await helper_call(
            request,
            "user.guest_link",
            {
                "event_id": int(event_id) if event_id else None,
                "permission": permission,
                "hours": int(hours or 24),
                "max_uses": int(max_uses) if max_uses else None,
            },
        )
        users_response = await helper_call(request, "user.list")
    except HTTPException as exc:
        return _error("/users", exc)
    return render(
        request,
        "users.html",
        page="users",
        users=users_response.data or [],
        roles=ROLES,
        guest_link=response.data,
        ok="guest link created",
        error="",
    )


# ---------------------------------------------------------------------------
# events / access
# ---------------------------------------------------------------------------


async def _events_data(request: Request) -> dict[str, Any]:
    response = await helper_call(request, "event.list")
    return response.data if isinstance(response.data, dict) else {}


@router.get("/events")
async def events_page(request: Request) -> Any:
    require_admin(request)
    data = await _events_data(request)
    return render(
        request,
        "events.html",
        page="events",
        data=data,
        ok=request.query_params.get("ok", ""),
        error=request.query_params.get("error", ""),
    )


@router.post("/events")
async def events_create(
    request: Request,
    name: str = Form(""),
    event_date: str = Form(""),
    slug: str = Form(""),
    notes: str = Form(""),
    activate: str = Form("1"),
    csrf_token: str = Form(""),
) -> Any:
    verify_csrf(request, csrf_token)
    try:
        await helper_call(
            request,
            "event.create",
            {
                "name": name,
                "event_date": event_date or None,
                "slug": slug,
                "notes": notes,
                "activate": activate == "1",
            },
        )
    except HTTPException as exc:
        return _error("/events", exc)
    return flash_redirect("/events", ok=f"created {name}")


@router.post("/events/{slug}/activate")
async def events_activate(
    request: Request, slug: str, csrf_token: str = Form("")
) -> Any:
    verify_csrf(request, csrf_token)
    try:
        await helper_call(request, "event.activate", {"slug": slug})
    except HTTPException as exc:
        return _error("/events", exc)
    return flash_redirect("/events", ok=f"{slug} is active")


@router.post("/events/{slug}/archive")
async def events_archive(
    request: Request, slug: str, csrf_token: str = Form("")
) -> Any:
    verify_csrf(request, csrf_token)
    try:
        await helper_call(request, "event.archive", {"slug": slug})
    except HTTPException as exc:
        return _error("/events", exc)
    return flash_redirect("/events", ok=f"{slug} archived")


@router.post("/events/{slug}/restore")
async def events_restore(
    request: Request, slug: str, csrf_token: str = Form("")
) -> Any:
    verify_csrf(request, csrf_token)
    try:
        await helper_call(request, "event.restore", {"slug": slug})
    except HTTPException as exc:
        return _error("/events", exc)
    return flash_redirect("/events", ok=f"{slug} restored")


@router.get("/access")
async def access_page(request: Request) -> Any:
    require_admin(request)
    data = await _events_data(request)
    config = await _config(request)
    return render(
        request,
        "access.html",
        page="access",
        data=data,
        config=config,
        permissions=PERMISSIONS,
        ok=request.query_params.get("ok", ""),
        error=request.query_params.get("error", ""),
    )


@router.post("/access")
async def access_update(
    request: Request,
    user_id: int = Form(0),
    event_id: int = Form(0),
    permission: str = Form("none"),
    csrf_token: str = Form(""),
) -> Any:
    verify_csrf(request, csrf_token)
    try:
        await helper_call(
            request,
            "event.access",
            {
                "user_id": user_id,
                "event_id": event_id,
                "permission": permission,
            },
        )
    except HTTPException as exc:
        return _error("/access", exc)
    return flash_redirect("/access", ok="access updated")


@router.post("/access/protocols")
async def access_protocols(
    request: Request,
    smb: str = Form("0"),
    copyparty: str = Form("0"),
    airdrop: str = Form("0"),
    csrf_token: str = Form(""),
) -> Any:
    verify_csrf(request, csrf_token)
    patch = {
        "fileserver_smb": {"enabled": smb == "1"},
        "fileserver_copyparty": {"enabled": copyparty == "1"},
        "airdrop": {"enabled": airdrop == "1"},
    }
    try:
        await helper_call(request, "config.patch", {"patch": patch})
        await helper_call(request, "apply")
    except HTTPException as exc:
        return _error("/access", exc)
    return flash_redirect("/access", ok="protocols updated")


# ---------------------------------------------------------------------------
# hotspot
# ---------------------------------------------------------------------------


@router.get("/hotspot")
async def hotspot_page(request: Request) -> Any:
    require_admin(request)
    status = (await helper_call(request, "hotspot.status")).data or {}
    config = await _config(request)
    return render(
        request,
        "hotspot.html",
        page="hotspot",
        status=status,
        config=config,
        ok=request.query_params.get("ok", ""),
        error=request.query_params.get("error", ""),
    )


@router.post("/hotspot")
async def hotspot_save(
    request: Request,
    ssid: str = Form(""),
    password: str = Form(""),
    country: str = Form(""),
    band: str = Form("auto"),
    channel: int = Form(0),
    subnet: str = Form(""),
    dhcp_start: str = Form(""),
    dhcp_end: str = Form(""),
    hidden: str = Form("0"),
    client_isolation: str = Form("0"),
    csrf_token: str = Form(""),
) -> Any:
    verify_csrf(request, csrf_token)
    hotspot = {
        "ssid": ssid,
        "country_code": country.upper(),
        "band": band,
        "channel": int(channel),
        "subnet": subnet,
        "dhcp_start": dhcp_start,
        "dhcp_end": dhcp_end,
        "hidden": hidden == "1",
        "client_isolation": client_isolation == "1",
    }
    if password:
        hotspot["password"] = password
    try:
        await helper_call(request, "config.patch", {"patch": {"hotspot": hotspot}})
        await helper_call(request, "apply")
    except HTTPException as exc:
        return _error("/hotspot", exc)
    return flash_redirect("/hotspot", ok="hotspot updated")


@router.post("/hotspot/kick")
async def hotspot_kick(
    request: Request, mac: str = Form(""), csrf_token: str = Form("")
) -> Any:
    verify_csrf(request, csrf_token)
    try:
        await helper_call(request, "hotspot.kick", {"mac": mac})
    except HTTPException as exc:
        return _error("/hotspot", exc)
    return flash_redirect("/hotspot", ok=f"disconnected {mac}")


# ---------------------------------------------------------------------------
# network
# ---------------------------------------------------------------------------


@router.get("/network")
async def network_page(request: Request) -> Any:
    require_admin(request)
    status = (await helper_call(request, "network.status")).data or {}
    config = await _config(request)
    return render(
        request,
        "network.html",
        page="network",
        status=status,
        config=config,
        networks=None,
        ok=request.query_params.get("ok", ""),
        error=request.query_params.get("error", ""),
    )


@router.post("/network/scan")
async def network_scan(
    request: Request, interface: str = Form(""), csrf_token: str = Form("")
) -> Any:
    verify_csrf(request, csrf_token)
    try:
        networks = (await helper_call(request, "network.scan", {"interface": interface})).data
    except HTTPException as exc:
        return _error("/network", exc)
    status = (await helper_call(request, "network.status")).data or {}
    config = await _config(request)
    return render(
        request,
        "network.html",
        page="network",
        status=status,
        config=config,
        networks=networks or [],
        ok="scan complete",
        error="",
    )


@router.post("/network/connect")
async def network_connect(
    request: Request,
    ssid: str = Form(""),
    password: str = Form(""),
    csrf_token: str = Form(""),
) -> Any:
    verify_csrf(request, csrf_token)
    try:
        await helper_call(
            request, "network.connect", {"ssid": ssid, "password": password}
        )
    except HTTPException as exc:
        return _error("/network", exc)
    return flash_redirect("/network", ok=f"connected to {ssid}")


@router.post("/network/settings")
async def network_settings(
    request: Request,
    uplink: str = Form("auto"),
    internet_sharing: str = Form("0"),
    wifi_client_interface: str = Form("auto"),
    csrf_token: str = Form(""),
) -> Any:
    verify_csrf(request, csrf_token)
    patch = {
        "network": {
            "uplink": uplink,
            "internet_sharing": internet_sharing == "1",
            "wifi_client_interface": wifi_client_interface,
        }
    }
    try:
        await helper_call(request, "config.patch", {"patch": patch})
        await helper_call(request, "apply")
    except HTTPException as exc:
        return _error("/network", exc)
    return flash_redirect("/network", ok="network settings applied")


@router.post("/network/test")
async def network_test(request: Request, csrf_token: str = Form("")) -> Any:
    verify_csrf(request, csrf_token)
    try:
        response = await helper_call(request, "network.test")
    except HTTPException as exc:
        return _error("/network", exc)
    return flash_redirect("/network", ok=response.message or "internet reachable")


@router.post("/network/disconnect")
async def network_disconnect(request: Request, csrf_token: str = Form("")) -> Any:
    verify_csrf(request, csrf_token)
    try:
        await helper_call(request, "network.disconnect")
    except HTTPException as exc:
        return _error("/network", exc)
    return flash_redirect("/network", ok="wifi uplink disconnected")


# ---------------------------------------------------------------------------
# airdrop
# ---------------------------------------------------------------------------


@router.get("/airdrop")
async def airdrop_page(request: Request) -> Any:
    require_admin(request)
    status = (await helper_call(request, "airdrop.status")).data or {}
    config = await _config(request)
    return render(
        request,
        "airdrop.html",
        page="airdrop",
        status=status,
        config=config,
        doctor=None,
        ok=request.query_params.get("ok", ""),
        error=request.query_params.get("error", ""),
    )


@router.post("/airdrop")
async def airdrop_save(
    request: Request,
    enabled: str = Form("0"),
    device_name: str = Form(""),
    receive_mode: str = Form("everyone"),
    interface: str = Form("auto"),
    target_follows_active_event: str = Form("1"),
    csrf_token: str = Form(""),
) -> Any:
    verify_csrf(request, csrf_token)
    patch = {
        "airdrop": {
            "enabled": enabled == "1",
            "device_name": device_name,
            "receive_mode": receive_mode,
            "interface": interface,
            "target_follows_active_event": target_follows_active_event == "1",
        }
    }
    try:
        await helper_call(request, "config.patch", {"patch": patch})
        await helper_call(request, "apply")
    except HTTPException as exc:
        return _error("/airdrop", exc)
    return flash_redirect("/airdrop", ok="AirDrop settings applied")


@router.post("/airdrop/doctor")
async def airdrop_doctor(request: Request, csrf_token: str = Form("")) -> Any:
    verify_csrf(request, csrf_token)
    status = (await helper_call(request, "airdrop.status")).data or {}
    config = await _config(request)
    try:
        doctor = (await helper_call(request, "airdrop.doctor")).data
    except HTTPException as exc:
        return _error("/airdrop", exc)
    return render(
        request,
        "airdrop.html",
        page="airdrop",
        status=status,
        config=config,
        doctor=doctor,
        ok="",
        error="",
    )


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------


@router.get("/storage")
async def storage_page(request: Request) -> Any:
    require_admin(request)
    data = (await helper_call(request, "storage.usage")).data or {}
    config = await _config(request)
    return render(
        request,
        "storage.html",
        page="storage",
        data=data,
        config=config,
        ok=request.query_params.get("ok", ""),
        error=request.query_params.get("error", ""),
    )


@router.post("/storage/mount")
async def storage_mount(request: Request, csrf_token: str = Form("")) -> Any:
    verify_csrf(request, csrf_token)
    try:
        response = await helper_call(request, "storage.mount")
    except HTTPException as exc:
        return _error("/storage", exc)
    return flash_redirect("/storage", ok=response.message)


@router.post("/storage/unmount")
async def storage_unmount(request: Request, csrf_token: str = Form("")) -> Any:
    verify_csrf(request, csrf_token)
    try:
        response = await helper_call(request, "storage.unmount")
    except HTTPException as exc:
        return _error("/storage", exc)
    return flash_redirect("/storage", ok=response.message)


@router.post("/storage/format")
async def storage_format(
    request: Request,
    fstype: str = Form("ext4"),
    confirmation: str = Form(""),
    label: str = Form("EVENTHUB"),
    csrf_token: str = Form(""),
) -> Any:
    verify_csrf(request, csrf_token)
    try:
        response = await helper_call(
            request,
            "storage.format",
            {"fstype": fstype, "confirmation": confirmation, "label": label},
        )
    except HTTPException as exc:
        return _error("/storage", exc)
    return flash_redirect("/storage", ok=response.message)


# ---------------------------------------------------------------------------
# system
# ---------------------------------------------------------------------------


@router.get("/system")
async def system_page(request: Request) -> Any:
    require_admin(request)
    overview = (await helper_call(request, "system.overview")).data or {}
    config = await _config(request)
    backups = (await helper_call(request, "backup.list")).data or []
    logs = request.query_params.get("log")
    log_text = ""
    if logs:
        response = await helper_call(request, "system.logs", {"name": logs, "lines": 300})
        log_text = response.data or ""
    return render(
        request,
        "system.html",
        page="system",
        overview=overview,
        config=config,
        backups=backups,
        log_name=logs or "",
        log_text=log_text,
        ok=request.query_params.get("ok", ""),
        error=request.query_params.get("error", ""),
    )


@router.post("/system/settings")
async def system_settings(
    request: Request,
    hostname: str = Form(""),
    timezone: str = Form(""),
    csrf_token: str = Form(""),
) -> Any:
    verify_csrf(request, csrf_token)
    patch = {"hostname": hostname, "timezone": timezone}
    try:
        await helper_call(request, "config.patch", {"patch": patch})
        await helper_call(request, "apply")
    except HTTPException as exc:
        return _error("/system", exc)
    return flash_redirect("/system", ok="system settings applied")


@router.post("/system/action")
async def system_action(
    request: Request,
    action: str = Form(""),
    confirm: str = Form("0"),
    csrf_token: str = Form(""),
) -> Any:
    verify_csrf(request, csrf_token)
    if action not in {"reboot", "shutdown"}:
        return _error("/system", HTTPException(status_code=400, detail="unknown action"))
    if confirm != "1":
        return _error("/system", HTTPException(status_code=400, detail="confirmation required"))
    try:
        response = await helper_call(
            request, f"system.{action}", {"confirm": True}
        )
    except HTTPException as exc:
        return _error("/system", exc)
    return flash_redirect("/system", ok=response.message)


@router.post("/system/export")
async def system_export(request: Request, csrf_token: str = Form("")) -> Any:
    verify_csrf(request, csrf_token)
    try:
        response = await helper_call(request, "backup.export", {"include_events": True})
    except HTTPException as exc:
        return _error("/system", exc)
    path = response.data.get("path") if isinstance(response.data, dict) else ""
    return flash_redirect("/system", ok=f"backup written to {path}")


@router.post("/system/import")
async def system_import(
    request: Request, path: str = Form(""), csrf_token: str = Form("")
) -> Any:
    verify_csrf(request, csrf_token)
    try:
        await helper_call(request, "backup.restore", {"path": path})
    except HTTPException as exc:
        return _error("/system", exc)
    return flash_redirect("/system", ok="backup restored")


@router.post("/system/update")
async def system_update(
    request: Request, check_only: str = Form("0"), csrf_token: str = Form("")
) -> Any:
    verify_csrf(request, csrf_token)
    try:
        response = await helper_call(
            request,
            "system.update",
            {"check_only": check_only == "1", "yes": check_only != "1"},
        )
    except HTTPException as exc:
        return _error("/system", exc)
    return flash_redirect("/system", ok=response.message)


@router.post("/system/recovery")
async def system_recovery(
    request: Request,
    action: str = Form(""),
    csrf_token: str = Form(""),
) -> Any:
    verify_csrf(request, csrf_token)
    actions = {
        "admin": "recovery.admin_password",
        "hotspot": "recovery.hotspot",
        "detect": "recovery.detect",
    }
    helper_action = actions.get(action)
    if helper_action is None:
        return _error("/system", HTTPException(status_code=400, detail="unknown recovery action"))
    try:
        response = await helper_call(request, helper_action, {})
    except HTTPException as exc:
        return _error("/system", exc)
    details = response.data if isinstance(response.data, dict) else {}
    if details.get("password"):
        overview = (await helper_call(request, "system.overview")).data or {}
        config = await _config(request)
        backups = (await helper_call(request, "backup.list")).data or []
        return render(
            request,
            "system.html",
            page="system",
            overview=overview,
            config=config,
            backups=backups,
            log_name="",
            log_text="",
            recovery_result=details,
            ok=response.message,
            error="",
        )
    return flash_redirect("/system", ok=response.message)


@router.post("/system/selftest")
async def system_selftest(request: Request, csrf_token: str = Form("")) -> Any:
    verify_csrf(request, csrf_token)
    try:
        response = await helper_call(request, "selftest.run")
    except HTTPException as exc:
        return _error("/system", exc)
    return flash_redirect("/system", ok=response.message)


@router.get("/logs/{name}", response_class=PlainTextResponse)
async def logs(request: Request, name: str) -> Any:
    require_admin(request)
    response = await helper_call(request, "system.logs", {"name": name, "lines": 500})
    return PlainTextResponse(str(response.data or ""))
