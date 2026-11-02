# network

Role-to-interface mapping, uplink selection/failover, nftables NAT and wifi
client mode.

## Config (`network:` section)

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Disable the module without touching the rest. |
| `uplink` | `auto` | `auto`, `wan`, `wifi` or `none`. |
| `internet_sharing` | `true` | NAT online uplink traffic for AP/LAN clients. |
| `wifi_client_interface` | `auto` | Concrete wifi client interface, or `none`. |
| `wifi_networks` | `[]` | Saved SSID/password/priority entries. |
| `connectivity_urls` | 2 HTTP URLs | Probes used to decide whether the uplink is online. |
| `connectivity_timeout` | `4` | Seconds per probe. |
| `connectivity_interval` | `30` | Monitor loop period. |
| `dns_servers` | `1.1.1.1`, `9.9.9.9` | Offered to hotspot clients. |
| `nft_table` | `eventhub` | nftables table name. |
| `forward_wan_lan` | `false` | Reserved; forwarding to the WAN LAN is never enabled. |

Roles come from `roles:` and are resolved as: explicit name → hardware
detection → `auto`/none.

## Behaviour

- `render()` produces `/etc/eventhub/nftables.conf` and
  `/etc/sysctl.d/90-eventhub.conf`.
- Input policy is drop. Only AP and LAN may reach dashboard, copyparty, SMB,
  DNS, DHCP, mDNS and SSH. WAN may only answer outbound traffic and DHCP.
- Forward policy only allows AP/LAN → WAN when `internet_sharing` is true.
  Masquerading prevents clients from reaching the uplink's own LAN.
- `eventhub-network.service` runs `eventhub network monitor`, which re-probes
  every `connectivity_interval` seconds, rewrites the rules on change and
  publishes `network.uplink_changed` on the event bus.
- Wifi client mode uses `wpa_supplicant` + `dhclient`/`udhcpc`.

## Testing

Unit tests cover rendering, nft invocations, health, uplink selection and iw
scan parsing with fake adapters.  Real radios, regulatory domains, carrier
detection and DHCP can only be verified on the Pi.
