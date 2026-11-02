# Design decisions

This file records the choices made where the plan left room for judgement.

## 2026-11-02 — Configuration format is YAML with pydantic v2

- One file, `/etc/eventhub/config.yaml` (override with `EVENTHUB_CONFIG`).
- Every section is a strict pydantic model (`extra="forbid"`), so typos are
  rejected rather than silently ignored.
- Each service section has an `enabled` flag.  A disabled module's validation
  errors are downgraded to warnings, so a bad optional module cannot stop the
  rest of the appliance from booting.
- Secrets use `SecretStr`.  Defaults are generated with `secrets.token_urlsafe`
  and `generate_wifi_password`, never a shared default password.
- The config is written atomically with mode `0600`.

## 2026-11-02 — Module discovery by convention

`ServiceRegistry` imports `eventhub.<name>.service` and reads its module-level
`SERVICE` class.  Third-party modules can register an `eventhub.services` entry
point.  The static `SERVICE_ORDER` catalogue only defines apply order; module
contents and install metadata are owned by the module itself.

## 2026-11-02 — Apply is snapshot-based, not in-place

`ApplyEngine` validates everything and renders every file before writing a
single byte.  It snapshots all targets (including "file did not exist") and
restores them if any write or service reload fails.  This satisfies the plan's
"steps either complete or roll back" requirement without needing a second
configuration tree.

## 2026-11-02 — SQLite owns dynamic state, YAML owns declarations

Users, password hashes, event rows, the access matrix, sessions, guest links,
audit history and service installation state live in SQLite.  Rendered service
configuration is never edited by hand; it is regenerated from YAML + DB state.
The active event is stored in the YAML config because it changes service
configuration (AirDrop target, default upload volume) and must be rendered.

## 2026-11-02 — OS access is always injected

Subprocess execution, filesystem access, systemd and time are small adapter
interfaces in `eventhub.core.adapters`.  No module calls `subprocess` or
`pathlib` directly.  This is what makes the full test suite run on a laptop.

## 2026-11-02 — Python 3.11 is the compatibility floor

The development venv here runs a newer Python, but the project uses only
3.11-compatible syntax (`StrEnum`, `tomllib`, `X | Y` unions) because
Raspberry Pi OS Bookworm ships 3.11.  A CI matrix should include 3.11 and 3.13.
