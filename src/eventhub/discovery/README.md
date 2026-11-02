# discovery

Avahi mDNS hostname and service advertisement.

## Config (`discovery:` section)

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | |
| `hostname` | `eventhub` | Advertised as `<hostname>.local`. |
| `domain` | `local` | |
| `advertise_smb` | `true` | Publish `_smb._tcp` port 445. |
| `advertise_copyparty` | `true` | Publish `_http._tcp` on the copyparty port. |
| `advertise_dashboard` | `true` | Publish `_http._tcp` on the dashboard port. |
| `interfaces` | `[]` | Empty means AP + LAN; WAN is never advertised by default. |
| `publish_ipv4` / `publish_ipv6` | `true` / `false` | |

## Files

- `/etc/avahi/avahi-daemon.conf` (generated; snapshot/rollback protected).
- `/etc/avahi/services/eventhub-smb.service`
- `/etc/avahi/services/eventhub-copyparty.service`
- `/etc/avahi/services/eventhub-dashboard.service`

`apply()` removes service files for protocols that were disabled, sets the
hostname with `hostnamectl` when available, and restarts `avahi-daemon`.

## Limitations

- Overwriting `avahi-daemon.conf` replaces distro customisations; use
  `discovery.interfaces` to control advertisement.
- Avahi does not run on `awdl0`; owly handles its own mDNS on that link.
- `avatar`/macOS SMB discovery uses `_smb._tcp`; Finder may still need SMB
  credentials entered once.
- Real resolution from macOS/Windows can only be verified on the Pi.
