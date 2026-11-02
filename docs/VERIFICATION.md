# Verification report

## What the automated suite verifies

`pytest` runs entirely on a laptop with fake subprocess, filesystem, systemd,
clock, network and hardware adapters.

- 178 tests pass (`ruff check .` and `mypy` also pass).
- Core: config validation/generated secrets/YAML round-trip, SQLite migrations
  and foreign keys, audit/settings/sessions, event bus, apply engine success,
  dry-run, validation/render failure, snapshot rollback, snapshot restore, CLI
  status/modules/config/apply.
- Installer: Debian/unknown distro detection, package mapping, every install
  step with fakes, idempotent install, answers file, update check, uninstall
  modes and typed full-wipe confirmation, backup/restore, unit rendering.
- TUI: first-boot wizard vs installed menu, wizard navigation, modules table,
  status/logs, configure, backup/recovery/uninstall rendering, QR encoding.
- Network/hotspot: role resolution, nftables/sysctl rendering, sharing toggles,
  apply/stop/health, uplink selection/probe failure, iw scan parsing,
  wpa_supplicant connect, hostapd/dnsmasq config, clients/kick.
- Storage/events: drive detection, UUID mount-unit rendering/escaping, apply,
  eject, format confirmation/removable guard, event layout/activation/archive,
  ingest target, access overrides, storage-missing guard.
- Users/fileservers: argon2 + copyparty hashes, login/expiry/guest links,
  last-admin protection, groups, copyparty accounts/volumes/archived read-only,
  SMB share/ingest lists, passdb sync, health.
- AirDrop/discovery: owly radio.toml, active-event target, argv, apply/stop/
  health/doctor, installer build detection/failure disabling, Avahi config and
  service XML, advertisement removal.
- Helper/dashboard: allow-list enforcement, config secret masking and partial
  patch, user/event actions, real unix-socket round-trip, login, rate limiting,
  forced password change, CSRF, admin-only pages, every page rendered with fake
  helper data, static template rendering.
- System/recovery/selftest: metrics collection/writing, journald rendering,
  timezone/reboot guards, admin/hotspot reset, boot fallback config, report
  serialization and a passing restricted selftest.

## What can only be verified on the physical Pi

- Actual wifi AP bring-up, regulatory/channel behaviour, client DHCP/DNS, and
  WPA3 driver support.
- USB ethernet/WiFi adapter detection at boot, carrier/failover timing, wifi
  client association and DHCP.
- nftables/kernel behaviour under the real WAN/LAN topology (the rules and
  invocations are unit-tested, not packet-tested).
- USB hotplug, ext4/exFAT mount units, safe eject, formatting, SMART/free-space
  thresholds, and event I/O to the external drive.
- Samba client compatibility (macOS Finder, Windows Explorer) and real
  throughput; SMB write/delete semantics per share.
- copyparty thumbnail/RAW/HEVC codecs, resumable uploads and real browser auth.
- owly/AirDrop: supported adapter/driver, owlyd D-Bus/polkit, monitor-mode
  injection, AWDL timing, actual iPhone/Mac transfer and arm64 build time.
- systemd boot ordering, journald limits, metrics service, undervoltage/UPS
  reporting, reboot/shutdown, and the documented overlay-root toggle.
- Password/credential flows against real Samba passdb and copyparty.
- Dashboard/phone rendering, real LAN access, TLS/proxy setups, and the helper
  `SO_PEERCRED` path under systemd sandboxing.

All hardware-sensitive code was written against explicit upstream documentation
(copyparty config format and argon2 hashing, t4t5/owly v0.4.2 build/run, Samba
smb.conf options) and is marked accordingly in the module READMEs.
