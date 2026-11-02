# systemd units

The units EventHub owns are rendered from `templates/systemd/*.j2` by
`eventhub.installer.units` and written to `/etc/systemd/system`.

| Unit | Purpose |
| --- | --- |
| `eventhub-helper.service` | root helper socket |
| `eventhub-dashboard.service` | unprivileged FastAPI dashboard |
| `eventhub-copyparty.service` | copyparty |
| `eventhub-hostapd.service` | our hostapd config |
| `eventhub-dnsmasq.service` | our dnsmasq config |
| `eventhub-network.service` | nftables/uplink monitor |
| `eventhub-radio.service` | owly AWDL lease |
| `eventhub-airdrop.service` | owly receive |
| `eventhub-metrics.service` | metrics/power daemon |
| `eventhub-recovery.service` | boot config validation/fallback |

Distro units enabled but not overwritten: `smbd.service` (or `smb.service` on
Arch), `avahi-daemon.service`, `owlyd.service`.

Storage generates escaped `.mount` units.  All long-running units run from the
venv entry point, never a shell string, with `NoNewPrivileges`, `ProtectHome`,
`ProtectSystem` and the narrowest `ReadWritePaths` that module needs.
