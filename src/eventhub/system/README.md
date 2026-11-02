# system

Metrics/power daemon, journald tuning, timezone, reboot/shutdown, recovery and
the on-device self-test.

## Config (`system:` section)

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | |
| `journald_max_use` | `128M` | `SystemMaxUse`. |
| `journald_keep_free` | `512M` | `SystemKeepFree`. |
| `journald_max_file_size` | `16M` | Rotation size limit. |
| `log_retention_days` | `14` | `MaxRetentionSec`. |
| `throttle_monitor` / `power_warning` | `true` | Surface undervoltage/throttle. |
| `metrics_interval` | `10` | Sample period. |
| `overlay_root` | `false` | Documented read-mostly-root toggle (manual). |
| `update_repo` / `update_branch` | GitHub | Update source. |
| `automatic_reboot` | `false` | Reserved for unattended updates. |

`eventhub-metrics.service` writes `/run/eventhub/metrics.json` every interval
with CPU temperature, load, memory, uptime, throttle flags and UPS/power-supply
entries.  The helper and dashboard also collect metrics on demand.

## Recovery

- `eventhub-recovery.service` runs at boot before network/helper/dashboard and
  validates the config.  If it cannot be loaded, it writes a generated
  `EventHub-Recovery` hotspot config and credentials to
  `/var/lib/eventhub/recovery-hotspot.txt`.
- `eventhub recovery --action reset-admin` resets the first admin.
- `eventhub recovery --action reset-hotspot` sets the known-good SSID
  `EventHub-Recovery`.
- `eventhub recovery --action detect-hardware` prints a fresh hardware report.

## Self-test

`eventhub selftest` checks config validation, every enabled service, the helper
socket, admin existence, storage mount/free space, events root/active event,
nftables table, uplink, listening ports (dashboard/copyparty/SMB/DNS/DHCP),
Avahi, AirDrop/AWDL and metrics.  Missing probe tools are reported as skipped,
not failed.  `--json` gives machine-readable output.

## Limitations

- Journald sizes are validated by systemd, not by EventHub.
- Overlay root is a manual toggle; EventHub does not edit the boot cmdline.
- UPS data depends on kernel `power_supply` entries.
- Real throttling, UPS, reboot and systemd ordering require the Pi.
