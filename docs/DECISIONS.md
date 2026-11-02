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

## 2026-11-02 — Installer steps are idempotent and testable

The installer never calls `subprocess` or `pathlib` directly; it uses the same
`Runtime` adapters as every module.  Steps return `StepResult` values and can
run in `dry_run`.  The first-run wizard renders its review screen before
changing anything, then hands the same answers to `Installer.install`.

Cancellation is cooperative: the TUI sets an event, and the installer checks
it between steps.  A cancelled install restores the previous config file.  It
does not attempt to uninstall system packages, because doing so is riskier than
leaving them installed for the next repair run.

## 2026-11-02 — Bootstrap is the only shell

`bootstrap.sh` installs git/curl/python3/venv, finds a local checkout or clones
the repo, creates `/opt/eventhub/venv`, installs the wheel, then `exec`s the
Python TUI.  When run through `curl | bash`, it reattaches stdin to `/dev/tty`;
if there is no terminal it falls back to `eventhub install --yes`.  The
repository URL and branch are environment-overridable.

## 2026-11-02 — Systemd units are rendered from Jinja templates

Units are installed into `/etc/systemd/system` and enabled only for modules
that are both available in the build and enabled in config.  Distro units
(hostapd, dnsmasq, smbd, avahi) are enabled but never overwritten.  Unit files
run the installed venv entry point, never a shell command string.

