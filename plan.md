# Task: Build "EventHub", a portable Raspberry Pi NAS and transfer hub for event photo/video shoots

You are building a complete, installable, headless Linux appliance. Work autonomously, in phases, and commit as you go. Where something is ambiguous, pick the most robust option, document the decision in `docs/DECISIONS.md`, and keep going. Only stop to ask me if you hit a true blocker. Do not claim something works unless it was actually tested; clearly mark anything that can only be verified on the physical hardware.


## 1. Purpose and users

The Pi lives in a bag at an event and creates its own wifi network. It acts as a file hub for photographers and editors.

- **Editor**: one person, connected by wired ethernet, who edits directly off the NAS.

- **Shooter**: an occasional wireless user who dumps photos and video onto the hub (phone or camera via wifi).

- **Puller**: one person who occasionally pulls photos or files down.

Everything must work with no internet. If internet is available, the hub shares it with hotspot clients.

## 2. Hardware (detect, don't hardcode)

- Raspberry Pi (assume a 4B, but detect the model and adapt).

- Onboard wifi is the **hotspot (AP)** interface.

- **USB wifi adapter** is dedicated to AirDrop (omdrop-owl). It needs monitor mode and injection support. Detect it by driver/USB ID, and make the interface assignment configurable.

- **USB ethernet dongle** is the preferred WAN uplink. The Pi's built-in ethernet port is the wired LAN for the editor.

- External USB SSD or HDD for storage. Support ext4 and exFAT, mount by UUID, and auto-mount on boot. Handle the drive being missing or unplugged gracefully and show it in the dashboard.

- Never hardcode `wlan0`, `eth0`, and so on. Use udev rules or persistent naming, and keep the role-to-interface mapping in config (`ap`, `airdrop`, `wan`, `lan`).

## 3. Required services

1. **Wifi hotspot**: hostapd plus dnsmasq (DHCP and DNS). Configurable SSID, WPA2/WPA3 password, band, channel, and country code. Hidden SSID is optional.

2. **Uplink and routing**: automatic failover between the WAN ethernet dongle and wifi client mode (if available). If an uplink has internet, NAT it to hotspot and LAN clients with nftables. If not, run fully offline. The dashboard must show the current uplink state.

3. **copyparty** as the main web file server and uploader: resumable uploads, a web UI, per-user and per-volume permissions, and a photo-friendly thumbnail/preview config.

4. **SMB** via Samba: shares map to events, with per-user permissions. Compatible with macOS, Windows, and Linux, and tuned for large RAW and video files.

5. **AirDrop receive** via omdrop-owl (the t4t5 project, so verify the correct upstream repo and build instructions). iPhones and Macs should be able to send photos to the hub. Received files land in the active event's ingest folder. Document the known limitations (AWDL, owl requirements, adapter compatibility, Pi kernel version) in `docs/AIRDROP.md`. The installer must detect and clearly warn if the adapter is unsupported rather than failing silently.

6. **mDNS**: Avahi, so the hub is reachable as `eventhub.local`, advertising SMB.

7. **Dashboard** (section 5), served over HTTP on the hotspot at `http://eventhub.local` and `http://10.42.0.1` (use a configurable subnet).

All services start on boot via systemd, restart on failure, and come up in the correct order with correct dependencies.


## 4. Architecture: Python, strictly modular

All application code is **Python 3.11+** (stay compatible with what Raspberry Pi OS Bookworm ships). Bash is allowed only for `bootstrap.sh` and thin systemd or udev glue. Everything else, including the installer logic, helper, TUI, dashboard, and service controllers, is Python.

### Module rules

- The project is a Python package, `eventhub`, with **one subpackage per concern**. Each module owns exactly one responsibility and talks to the others only through a small, documented interface (an abstract base class or Protocol plus typed dataclasses or pydantic models). No module imports another module's internals.

- Modules:

  - `core`: config loading and validation (pydantic), the SQLite layer, logging, the event bus, and shared types.

  - `hotspot`: hostapd and dnsmasq config rendering and control.

  - `network`: interface detection and role mapping, uplink selection and failover, NAT and nftables rules, and wifi client scanning and connect.

  - `storage`: drive detection, mount/unmount by UUID, the events folder model, and space monitoring.

  - `users`: user, group, and role management, which syncs to each protocol backend.

  - `events`: event lifecycle (create, activate, archive), per-event access, and ingest layout.

  - `fileserver\_copyparty`: renders the copyparty config and controls the service.

  - `fileserver\_smb`: Samba config and passdb sync.

  - `airdrop`: omdrop-owl build, config, control, and the ingest hook.

  - `discovery`: Avahi and mDNS.

  - `system`: service status, logs, power and throttle monitoring, reboot and shutdown, and updates.

  - `helper`: the privileged root daemon.

  - `dashboard`: the web UI and API (FastAPI), unprivileged.

  - `tui`: the terminal UI (section 6).

  - `installer`: the idempotent install, update, repair, and uninstall steps.

- **Plugin-style service modules**: every service module (hotspot, copyparty, smb, airdrop, discovery) implements the same interface: `validate(config)`, `render(config) -\> files`, `apply()`, `start()`, `stop()`, `restart()`, `status() -\> ServiceStatus`, and `health\_check()`. The core discovers and orchestrates them through a registry, so adding a new service means adding one module and registering it, with no edits elsewhere.

- Each module has its own `README.md`, its own tests, and its own config schema section. A module can be disabled in config without breaking the rest of the system.

- The dashboard, TUI, and CLI never call system tools directly. They call module interfaces through the registry and helper, so the privilege boundary stays in one place and all three front ends behave identically.

- Use dependency injection for anything that touches the OS (subprocess, filesystem, netlink). Wrap them in small adapter classes so every module is unit-testable with fakes on a laptop, no Pi required.

### Tooling

- Packaging: `pyproject.toml`, installed into a dedicated venv at `/opt/eventhub/venv`.

- Typing and quality: full type hints, `mypy --strict` on `core` and the module interfaces, `ruff` for lint and format.

- Testing: `pytest`, with unit tests per module (mocked OS adapters) and a separate on-device `eventhub selftest` command.

- CLI: a single `eventhub` entry point (Typer or argparse) with subcommands such as `install`, `update`, `uninstall`, `apply`, `status`, `selftest`, `module \<name\> \<action\>`, and `recovery`. Running `eventhub` with no arguments opens the TUI.

- Dashboard: FastAPI plus Jinja2 templates with htmx or Alpine. No Node build step.

- Dependencies: keep them minimal and pinned in a lockfile.


## 5. Dashboard (web admin)

A lightweight, mobile-friendly, clean web UI with low RAM use.

**Single source of truth**: one config file (YAML or TOML) plus a small SQLite DB. The dashboard edits the config, and a privileged apply step renders the real configs (hostapd, dnsmasq, nftables, smb.conf, copyparty config, owl settings) from Jinja2 templates and reloads the services. Never edit service configs by hand. Applies are atomic with rollback on failure.

Pages and features:

- **Overview**: service status (green/red) for every service, uplink state, connected clients (wifi and wired), storage usage, CPU temp, throttling, uptime, and battery/UPS status if available.

- **Users**: create, edit, and delete users and groups, and set passwords. A user maps to copyparty, SMB, and dashboard login. Roles: `admin`, `editor` (read/write/delete), `shooter` (upload-only, no delete), and `viewer` (read-only). Support temporary guest accounts or links with an expiry.

- **Events**: create an event with a name and date, which creates the folder structure, copyparty volume, and SMB share. Set the **active event**, which is where AirDrop and default uploads land. Per-event access control, archive/close an event (read-only), and per-event size. Ingest layout such as `/events/\<event\>/ingest/\<user\>/`, `/raw`, and `/edit`.

- **Access**: a matrix view of users × events × permission level, plus protocol toggles (enable or disable SMB, copyparty, and AirDrop independently).

- **Hotspot**: SSID, password, band and channel, country code, client list with the ability to kick, and DHCP range and subnet.

- **Network and internet**: choose the uplink (auto, WAN dongle, or wifi client), scan and connect to wifi networks as the uplink, show the IP and gateway, run a connectivity test, and toggle internet sharing on or off.

- **AirDrop**: enable or disable, the visible device name, receive mode (everyone vs contacts-only if feasible), the target folder (follows the active event), and owl status and logs.

- **Storage**: detected drives, mount status, free space, a safe "eject/unmount" button, and a format wizard with heavy confirmation.

- **System**: hostname, timezone, a logs viewer per service, safe reboot and shutdown buttons, config export and import, and update.

- **Auth**: admin login required. Hash passwords with argon2 or bcrypt, set CSRF protection and secure cookies, and rate-limit login. The first-boot admin password is generated and printed to the console and a file, with a forced change on first login.

The dashboard process runs unprivileged and talks to the root `helper` (systemd service over a unix socket, exposing only a narrow whitelist of apply and control actions). No raw shell from the web.


## 6. Installation and management TUI

Running the install command (`curl ... | sudo bash` via `bootstrap.sh`, or `sudo eventhub` once installed) opens a **terminal UI** that is the single entry point for installing and managing the hub. No flags are needed for normal use.

### Bootstrap flow

- `bootstrap.sh` (the only non-Python file) detects the distro and package manager (Raspberry Pi OS and Debian/Ubuntu first, best effort for Fedora and Arch), installs the bare minimum (git, curl, python3, venv support), clones or pulls the repo from my GitHub URL (a variable at the top), creates `/opt/eventhub/venv`, installs the package, and **launches the TUI**.

- If the hub is not installed, the TUI opens on the first-time setup wizard. If it is installed, it opens on the management menu.

- When stdin is not a TTY (for example, piped over `curl | bash`), reattach to `/dev/tty` so the TUI still works. If no TTY is available at all, fall back to non-interactive mode.

### The `tui` module

- Built with **Textual** on top of Rich. Keyboard-driven with mouse support, working over SSH and serial consoles, and rendering sanely at 80x24.

- A thin presentation layer with **no business logic**. It calls the same module registry and `installer` interfaces as the CLI and dashboard.

- Long-running actions (installs, builds, updates) run in background workers with a live log pane, a progress indicator, and safe cancel. All output is also written to `/var/log/eventhub-install.log`, using clear `\[ok\]`, `\[warn\]`, and `\[fail\]` markers.

### Main menu

1. **Install / Repair**: first-time setup, or re-run to fix a broken install. Idempotent.

2. **Update**: pull the latest from GitHub, show current vs available version and a changelog summary, back up the config, apply, and offer to roll back on failure.

3. **Modules**: every module with its state (installed, enabled, running, healthy). Enable, disable, reinstall, or remove each one individually (for example, skip AirDrop if the adapter is unsupported).

4. **Configure**: interface and role mapping (AP, AirDrop, WAN, LAN), country code, hotspot basics, storage drive selection, and the dashboard URL with a QR code.

5. **Status and logs**: service health, a per-module log viewer, and the self-test with a pass/fail report.

6. **Backup and restore**: export or import config and the user DB, with optional event-metadata backup.

7. **Recovery**: reset the admin password, reset the hotspot to the known-good `EventHub-Recovery` defaults, and re-detect hardware.

8. **Uninstall**: choose between *remove services only* (keep data and config), *remove everything except event data*, and *full wipe*. Show exactly what will be removed and require a typed confirmation. **Never touch the storage drive's event data unless I explicitly pick the full wipe option and confirm a second time.**

9. **Quit**.

### First-time setup wizard

Steps with validation and a back button: hardware detection report (Pi model, USB wifi and ethernet adapters found, and a warning if the AirDrop adapter is unsupported), interface-to-role mapping, module selection, storage drive selection (mount by UUID, and offer to format only on explicit confirmation), hotspot SSID and password (generate a strong one by default), the admin account, and a final review screen before anything is changed. At the end, show the dashboard address, the credentials, and a QR code for joining the hotspot.

### Non-interactive mode

Everything the TUI can do is also available as CLI subcommands, so automation and re-runs work without a TUI: `eventhub install --yes`, `eventhub update`, `eventhub uninstall --mode services-only`, `eventhub module \<name\> enable|disable|reinstall`, and so on. Support `--config answers.yaml` for an unattended install.

### Safety

- Every destructive action shows a summary and asks for confirmation, and the default selection is always the safe one.

- Snapshot the changed config files before applying, so Update and Repair can roll back.

- Steps either complete or roll back; never leave the hub half-configured.

- Testable: use Textual's pilot test harness for screen flows, and run installer steps against fake OS adapters.


## 7. Reliability and event-day concerns

- Boots unattended to a fully working hub in under about 90 seconds, with no keyboard or monitor needed.

- Survives abrupt power loss. Use a journaled filesystem for storage, tune journald to limit SD wear, and offer a read-mostly (overlay) root filesystem as a documented toggle.

- Handle low-power and undervoltage warnings, and surface them in the dashboard.

- Fallback: if the config is broken, boot to a known-good default hotspot (`EventHub-Recovery`) so I can always get back in.

- Check disk space before ingest, and warn at 80% and 90%.

- Optional but nice: checksum verification on ingest and a duplicate-detection report.

## 8. Security

- No default passwords in the final state. Use strong generated secrets.

- The hotspot's WPA password is required. Never run it open.

- nftables firewall: hotspot clients can only reach the hub services and the internet (if shared), never the WAN network's LAN.

- Expose the dashboard and services only on the AP and LAN interfaces, never on the WAN side by default.

- Pin or verify upstream sources where possible. Keep dependencies minimal.


## 9. Repo structure and deliverables

```
bootstrap.sh              \# only non-Python entry point  
pyproject.toml  
src/eventhub/  
  core/  hotspot/  network/  storage/  users/  events/  
  fileserver\_copyparty/  fileserver\_smb/  airdrop/  discovery/  
  system/  helper/  dashboard/  tui/  installer/  
templates/                \# per-module config templates (Jinja2)  
systemd/  
tests/\<module\>/  
docs/
```

- `README.md` with quick start, a hardware list, and wiring and role mapping.

- `docs/ARCHITECTURE.md` (a diagram of interfaces, services, and data flow), per-module docs, `docs/AIRDROP.md`, `docs/DECISIONS.md`, and `docs/TROUBLESHOOTING.md`.

- Unit tests per module, plus `eventhub selftest`, which checks every service, port, mount, and route on a live device.

## 10. Phases (commit after each, with passing tests for the modules touched)

1. `core`, the module interface and registry, and the CLI skeleton.

2. `installer`, `tui` (menus for not-yet-built modules show "not available"), and `bootstrap.sh`.

3. `network` and `hotspot`.

4. `storage` and `events`.

5. `users`, `fileserver\_copyparty`, and `fileserver\_smb`.

6. `airdrop` and `discovery`.

7. `helper` and `dashboard`, wired to every module through the registry.

8. `system`, recovery mode, selftest, hardening, and docs.

At the end of each phase, report exactly what you verified, and what you could not verify without the physical hardware. Wire each new module into the TUI's Modules screen and the setup wizard as it lands.

