# dashboard & helper

The dashboard runs unprivileged and never executes system tools.  Every
mutation goes through the root helper over `/run/eventhub/helper.sock`.

## Privilege boundary

- `eventhub-helper.service` runs as root, reads the main config (mode `0600`),
  and exposes a fixed allow-list of high-level actions (`apply`, `status`,
  `user.create`, `event.activate`, `storage.unmount`, `system.reboot`, …).
- The socket is mode `0660` in a `0750 root:eventhub` directory.  `SO_PEERCRED`
  restricts callers to root, the dashboard user, the service user, or a member
  of the socket group.
- The helper never accepts an arbitrary command, executable path or shell
  string.  Actions validate their own payloads (usernames, slugs, MACs, ports,
  paths from detected drives).
- The dashboard's settings live in `/etc/eventhub/dashboard.json` (mode `0640`
  `root:eventhub-web`), so it does not need the main config.  It contains the
  session secret, bind settings, helper socket and DB path only.

## Dashboard

- FastAPI + Jinja2 + vendored htmx (no Node/CDN).  Mobile-friendly dark UI.
- Login with argon2 passwords; sessions are random IDs in SQLite, signed in a
  `HttpOnly`, `SameSite=Lax` cookie; CSRF tokens are required for every POST;
  login attempts are rate-limited per IP.
- The first-boot admin must change their password before any other page opens.
- Pages: overview, events, access matrix, users/guests, hotspot, network,
  AirDrop, storage and system.
- JSON endpoints `/healthz` and `/api/overview`; logs are available as
  `text/plain`.
- Host header guard: configured names plus any IP literal; WAN remains blocked
  by nftables.

## Config

`dashboard:` and `helper:` sections.  Default dashboard port is 80; the unit
gets `CAP_NET_BIND_SERVICE`.

## Testing

`tests/test_dashboard_helper.py` covers login, forced password change, CSRF,
admin-only pages, every page rendering with fake helper data, action dispatch,
config masking/patching, and a real unix-socket round-trip.  Browser rendering
on a phone, real TLS/proxy setups and systemd hardening can only be verified on
the Pi.

## Limitations

- Login rate limiting is per dashboard process (single uvicorn worker by
  design).
- The helper cannot restart itself from an `apply`; socket-path changes require
  a systemctl restart.
- The dashboard binds `0.0.0.0` and relies on nftables to keep WAN out.
