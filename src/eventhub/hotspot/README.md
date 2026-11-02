# hotspot

hostapd access point plus dnsmasq DHCP/DNS.  EventHub ships its own
`eventhub-hostapd.service` and `eventhub-dnsmasq.service` so it does not depend
on distro-specific default files, and stops the distro units if they are
already running.

## Config (`hotspot:` section)

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | |
| `ssid` | `EventHub` | 1–32 bytes. |
| `password` | generated | 8–63 characters; never open. |
| `country_code` | `GB` | Two-letter ISO code; sets the regulatory domain. |
| `band` | `auto` | `2.4`, `5` or `auto`. |
| `channel` | `0` | 0 = automatic/ACS. |
| `hw_mode` | `auto` | `g`, `a` or `auto`. |
| `auth_mode` | `wpa2-wpa3` | `wpa2`, `wpa3` or transition mode. |
| `hidden` | `false` | Hide the SSID. |
| `interface` | `auto` | Overrides `roles.ap` when set. |
| `subnet` | `10.42.0.0/24` | DHCP network. |
| `dhcp_start`/`dhcp_end` | `.10`/`.200` | Must be inside the subnet. |
| `lease_time` | `12h` | |
| `dns_domain` | `eventhub.local` | |
| `max_clients` | `64` | |
| `client_isolation` | `false` | AP isolation. |

## Files

- `/etc/hostapd/eventhub.conf` (mode `0600`, contains the WPA passphrase).
- `/etc/dnsmasq.d/eventhub.conf` (mode `0644`).

`dnsmasq` uses `bind-dynamic`, serves DHCP only on the AP, answers
`eventhub.local` locally and forwards configured upstream DNS only.

## Limitations

- WPA3/transition mode depends on the wifi driver and hostapd build.
- 5 GHz channel availability depends on the country code and DFS support.
- dnsmasq must own UDP/TCP 53; `systemd-resolved` or a distro dnsmasq can
  conflict.  The module stops the distro units, and `docs/TROUBLESHOOTING.md`
  covers the resolved case.
- The hotspot cannot be verified without a wifi radio; unit tests use fake
  hostapd/dnsmasq/systemd adapters.
