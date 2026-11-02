# EventHub

A portable Raspberry Pi NAS and transfer hub for event photo/video shoots.
It creates its own wifi network, serves a web dashboard at
`http://eventhub.local` (and `http://10.42.0.1`), exposes SMB and copyparty,
and receives AirDrop transfers — all without internet.

## Quick start

On a Raspberry Pi running Raspberry Pi OS Bookworm (64-bit recommended):

```sh
curl -fsSL https://raw.githubusercontent.com/jarrfarr/portapix-v2/master/bootstrap.sh | sudo bash
```

Set `OWNER` to your GitHub account, or override:

```sh
curl -fsSL .../bootstrap.sh | sudo EVENTHUB_REPO_URL=https://github.com/jarrfarr/portapix-v2 bash
```

From a checkout:

```sh
sudo ./bootstrap.sh
```

The installer opens a Textual TUI.  Non-interactive:

```sh
sudo eventhub install --yes
sudo eventhub selftest
sudo eventhub status
```

## Hardware and roles

| Piece | Role | Notes |
| --- | --- | --- |
| Raspberry Pi 4B/5 | host | Model is detected, not assumed. |
| Onboard wifi | `ap` | hostapd + dnsmasq hotspot. |
| USB wifi adapter | `airdrop` | Must support monitor mode/injection; used by owly. |
| USB ethernet dongle | `wan` | Preferred internet uplink. |
| Onboard ethernet | `lan` | Wired editor, full service access. |
| USB SSD/HDD | storage | ext4 or exFAT, mounted by UUID. |

Interfaces are never hardcoded.  Roles are explicit in config
(`roles.ap`, `roles.airdrop`, `roles.wan`, `roles.lan`) and auto-detected only
when set to `auto`.  The setup wizard shows the detection report and lets you
remap every role.  The AP and AirDrop interfaces must differ.

Wiring:

```
                    ┌──────────── Raspberry Pi ────────────┐
 iPhone/Mac --AWDL--│ USB wifi (monitor)  → AirDrop        │
 Shooters  --wifi-->│ onboard wifi        → hotspot (AP)   │
 Editor    --LAN--->│ onboard ethernet    → wired LAN      │
 Internet  --WAN--->│ USB ethernet dongle → NAT/failover   │
                    │ USB SSD/HDD         → events data    │
                    └──────────────────────────────────────┘
```

## What is included

- **Wifi hotspot** — hostapd + dnsmasq, WPA2/WPA3, configurable band/channel,
  generated password, hidden-SSID option, client list and kick.
- **Network** — role-aware interfaces, uplink failover, nftables NAT, wifi
  client scan/connect, no exposure on WAN.
- **Storage** — removable drive detection, UUID systemd mount units, free-space
  health, safe eject, ext4/exfat format with typed confirmation.
- **Events** — create/activate/archive, per-event ingest layout and access
  matrix; AirDrop/default uploads follow the active event.
- **Users** — admin/editor/shooter/viewer, groups, guest accounts and expiring
  guest links; SMB passdb sync and copyparty argon2 accounts.
- **SMB** — per-event shares, ingest share for shooters, tuned for large files.
- **copyparty** — resumable uploads, thumbnails, per-event volumes, read-only
  archives.
- **AirDrop** — t4t5/owly (formerly omdrop-owl) receiver with installer build
  and adapter warnings; see [docs/AIRDROP.md](docs/AIRDROP.md).
- **mDNS** — Avahi hostname and service advertisement.
- **Dashboard** — mobile-friendly FastAPI UI with admin auth, CSRF, rate
  limiting, sessions, users/events/network/storage/system pages.
- **Recovery** — known-good `EventHub-Recovery` hotspot if config breaks,
  admin password reset, hardware re-detection, live self-test.

## Repository layout

```
bootstrap.sh            only shell entry point
pyproject.toml
src/eventhub/           Python package, one subpackage per concern
templates/              Jinja2 service configs, systemd units, dashboard
tests/                  unit tests with fake OS adapters
docs/                   architecture, decisions, install, AirDrop, TUI, security
```

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/mypy
```

Everything except `bootstrap.sh` and thin systemd/udev glue is Python 3.11+.
All OS access goes through adapters, so the test suite runs on a laptop with no
Pi, root or systemd.

## Verification

The unit suite covers configuration, apply/rollback, every module with fake
adapters, installer steps, TUI screens, dashboard login/pages/CSRF, helper
socket round-trip and recovery/self-test.  Real wifi radios, AirDrop, SMB
clients, USB hotplug, formatting, Pi throttling and systemd boot ordering can
only be verified on the physical hardware; each phase report and
[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) call this out.
