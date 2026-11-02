# helper

Privileged root daemon for EventHub.  Runs as `eventhub-helper.service` and
listens on a unix socket (`/run/eventhub/helper.sock`).

## Protocol

One JSON object per line:

```json
{"action": "status", "payload": {}, "request_id": "optional"}
```

Response:

```json
{"ok": true, "message": "ok", "data": {...}, "request_id": "...", "elapsed_ms": 3}
```

The action list is in `helper.allowed_actions`; unknown or disallowed actions
are rejected before any module is touched.  Requests are size-limited and have
a configurable timeout.  Every action is audited in SQLite.

## Peer authentication

`SO_PEERCRED` is checked against root, the dashboard user, the service user and
the socket group.  Filesystem permissions are the first line of defence:
`/run/eventhub` is `0750 root:eventhub`, the socket is `0660`.

## Actions

`apply`, `status`, `overview`, `modules.list`, `config.get`, `config.patch`,
service start/stop/restart, module enable/disable, user CRUD/password/guest
links, event CRUD/access, storage drives/usage/mount/unmount/format, network
status/scan/connect/disconnect/test, hotspot status/clients/kick, AirDrop
status/doctor/target, system overview/logs/reboot/shutdown/export/import/update,
self-test, recovery, and backup export/list/restore.

`config.patch` deep-merges a partial mapping into the single YAML config; the
dashboard never sends the whole (secret-masked) config back.

## Testing

`tests/test_dashboard_helper.py` exercises dispatch, config secret masking,
patching, user/event actions and a real unix-socket client/server round-trip
with an injected peer checker.  The actual `SO_PEERCRED` path and systemd
hardening can only be verified on the Pi.
