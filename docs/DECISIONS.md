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

## 2026-11-02 — EventHub owns hostapd/dnsmasq units

Rather than editing `/etc/default/hostapd` and hoping Debian's unit reads it,
EventHub installs `eventhub-hostapd.service` and `eventhub-dnsmasq.service`
that point directly at `/etc/hostapd/eventhub.conf` and
`/etc/dnsmasq.d/eventhub.conf`.  This works the same on Debian, Fedora and
Arch.  If the distro's hostapd/dnsmasq units are active, the hotspot module
stops them first to avoid radio/port conflicts.  Samba and Avahi still use
their distro units because they have stable, multi-distro configuration paths.

## 2026-11-02 — The network unit is a monitor, not a one-shot

`eventhub-network.service` is a long-running process (`eventhub network
monitor`) that applies nftables/sysctl at boot, probes the uplink every
`connectivity_interval` seconds and rewrites rules only when the uplink or
roles change.  A one-shot unit plus timer would add ordering complexity and a
second unit to install; a single monitor makes failover observable and keeps
the dashboard's uplink state fresh.

## 2026-11-02 — Event data follows the selected storage drive

`events.root` is explicit when set.  Otherwise, if `storage.drive.uuid` is
configured, the event root is `<mount_point>/events`; with no drive it falls
back to `<data_root>/events`.  This means choosing a drive in the wizard moves
the event tree without adding another path setting.

## 2026-11-02 — Refuse event creation on an unmounted configured drive

Writing into `<mount_point>` while the real filesystem is unmounted creates
files on the root filesystem that become invisible (and confusing) once the
drive is attached.  `events.create` therefore raises a clear conflict when a
configured UUID is not present in `/proc/self/mounts`.  The storage module is
still allowed to be optional: if no drive is configured at all, the fallback
path is used.

## 2026-11-02 — Mounts are systemd units, not /etc/fstab edits

`storage.render()` emits an escaped `.mount` unit with `ConditionPathExists`
and `TimeoutSec`.  This gives auto-mount on boot, safe removal without boot
failure, and lets the apply engine snapshot/roll back the file.  `/etc/fstab`
is user-owned and too dangerous to rewrite wholesale.

## 2026-11-02 — Copyparty passwords are hashed, not plaintext

copyparty supports argon2 password verification with `--ah-alg argon2` and a
fixed salt.  EventHub stores a copyparty-compatible `+hash` per user, generated
with `fileserver_copyparty.password_salt`, so the generated config never
contains plaintext passwords.  If the salt is lost, users must reset their
passwords; this is the same tradeoff copyparty documents.

## 2026-11-02 — SMB and copyparty permissions cannot be identical

Samba can grant read or read-write per share, but not "upload without delete"
inside a share.  EventHub therefore gives shooters a write share only for the
event's `ingest` folder and keeps them out of the event root share.  copyparty
can express upload-only with `w`, so it enforces the stricter role inside the
event volume.  This asymmetry is documented in `fileserver_smb/README.md`
instead of claiming identical semantics.

## 2026-11-02 — User creation does not silently rewrite config

Users/groups/access are dynamic state in SQLite.  Creating or deleting a user
updates the SMB passdb immediately and returns; the caller (helper/CLI/TUI)
then re-renders the file-server configs via the registry.  This keeps the
privilege boundary and the apply/rollback path in one place.

## 2026-11-02 — AirDrop uses t4t5/owly (omdrop-owl renamed)

The upstream project is now `t4t5/owly`, a Rust implementation split into
`owlyd` (radio) and `owly` (AirDrop CLI).  EventHub pins `v0.4.2`, runs
`owly radio up` in a root unit and `owly receive` in an unprivileged unit, and
opens only TCP 8771 on `awdl0`.  v0.4.2 has no arm64 release asset, so the
installer builds from source and requires Rust 1.88+.  Failure to detect an
adapter or to build disables AirDrop with an explicit warning; it never fails
silently.

## 2026-11-02 — Avahi config is generated, not patched

`/etc/avahi/avahi-daemon.conf` is rendered from config and covered by the
apply snapshot.  Service records are separate XML files, so toggling SMB,
copyparty or dashboard advertisement removes exactly those files.  Avahi is
bound to the AP and LAN interfaces by default; WAN is never advertised.

## 2026-11-02 — mDNS and AirDrop use separate interfaces

Avahi advertises on the AP/LAN only.  Owly runs its own mDNS responder on
`awdl0` for AirDrop; mixing the two would break the AWDL timing/announcements.

## 2026-11-02 — The dashboard never reads the root config

The unprivileged dashboard gets a dedicated `/etc/eventhub/dashboard.json`
(mode `0640 root:eventhub-web`) rendered by the dashboard module.  It contains
only the session secret, bind/helper/db settings and is readable by the
dashboard group, not the full wifi/protocol secrets.  All configuration views
and mutations go through the helper; the dashboard only reads SQLite for
sessions and login verification.

## 2026-11-02 — Helper authentication is peer credentials + allow-list

There is no shared token to leak.  The helper checks `SO_PEERCRED` and allows
root, the dashboard/service users, or members of the socket group; the socket
directory is `0750` and the socket `0660`.  Actions are high-level and fixed;
the dashboard cannot submit a command, executable path, or arbitrary device.
Every action is audited in SQLite.

## 2026-11-02 — Sessions and CSRF use opaque IDs in SQLite

The cookie is a signed opaque session id (itsdangerous), not a JWT carrying
claims.  Revoking a session is a DB update, and the CSRF token lives beside it.
Login attempts are rate-limited per IP in process; the dashboard is intentionally
one uvicorn worker to keep that accounting simple and RAM use low.

## 2026-11-02 — htmx is vendored

The dashboard ships `htmx.min.js` under `templates/dashboard/static` instead of
loading a CDN, because the hub must work with no internet.  Forms are ordinary
HTML POSTs with server redirects; htmx is available for progressive enhancement
without a Node build step.

## 2026-11-02 — Recovery validates config before anything else

`eventhub-recovery.service` runs before network, hotspot, helper and dashboard.
If `/etc/eventhub/config.yaml` is unreadable/invalid it replaces it with a newly
generated `EventHub-Recovery` hotspot config and writes credentials to
`/var/lib/eventhub/recovery-hotspot.txt`.  A valid config is never modified.
Admin reset and hotspot reset are also available from the CLI/API, but the
boot check is the only path that can recover a completely broken file.

## 2026-11-02 — Self-test distinguishes fail from skip

`eventhub selftest` never reports a missing probe tool as a service failure.
Checks are pass/fail/skip; a skipped optional service (AirDrop, storage without
a drive, internet when offline) does not make the report fail.  Real failures
include config errors, disabled-but-required admin absence, missing mounts,
missing nftables table and services that are enabled but not running.

## 2026-11-02 — Metrics are collected in one core helper

`core.metrics.collect_metrics()` is shared by the metrics daemon, helper and
CLI.  It reads sysfs/proc through the injected filesystem and `vcgencmd` through
the runner, so every front end reports the same CPU temperature, load, memory,
throttle and UPS values.

## 2026-11-02 — Overlay root is documented, not auto-flipped

`system.overlay_root` is surfaced in the UI/self-test and documented, but the
installer does not rewrite the boot cmdline.  A wrong overlay configuration can
make an unattended hub unbootable; the operator must use the distro/Raspberry
Pi overlay tooling and reboot deliberately.

## 2026-11-02 — TUI is whiptail/raspi-config style, not Textual

The original plan called for Textual.  The operator explicitly asked for a
simple terminal installer in the style of `raspi-config`, so the TUI now uses
whiptail/dialog menus over newt, with a numbered plain-text fallback when
whiptail is unavailable.  This keeps the first-boot experience familiar on
Raspberry Pi OS, works over SSH/serial, and removes the Textual dependency and
its full-screen app model.  `bootstrap.sh` and the installer now install
whiptail/newt.  The TUI remains a thin presentation layer over `TuiController`.








