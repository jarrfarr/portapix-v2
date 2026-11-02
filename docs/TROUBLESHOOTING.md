# Troubleshooting

## The dashboard does not open

1. Join the hotspot `EventHub` (or the SSID you chose) and open
   `http://eventhub.local` or `http://10.42.0.1/`.
2. If mDNS does not resolve, use the IP.  Check:
   ```sh
   ip addr
   systemctl status eventhub-hostapd eventhub-dnsmasq
   journalctl -u eventhub-hostapd -u eventhub-dnsmasq -n 100
   ```
3. Check nftables did not lock you out:
   ```sh
   sudo nft list table inet eventhub
   ```
4. Dashboard/helper logs:
   ```sh
   journalctl -u eventhub-helper -u eventhub-dashboard -n 200
   ```
5. Reset the hotspot to the known-good recovery config:
   ```sh
   sudo eventhub recovery --action reset-hotspot
   ```
   It prints/writes `EventHub-Recovery` credentials to
   `/var/lib/eventhub/recovery-hotspot.txt`.

## Locked out of the dashboard

```sh
sudo eventhub recovery --action reset-admin
```

The new password is printed and written to
`/var/lib/eventhub/first-boot-admin.txt`.

## Config is broken / boot fallback

`eventhub-recovery.service` runs before network/helper/dashboard on every boot.
If `/etc/eventhub/config.yaml` cannot be loaded it replaces it with a generated
`EventHub-Recovery` hotspot config and writes the credentials to
`/var/lib/eventhub/recovery-hotspot.txt`.  Snapshots of previous configs are in
`/var/lib/eventhub/snapshots/`.

## A service fails after apply

```sh
sudo eventhub status
sudo eventhub selftest --json
sudo eventhub module <name> status
journalctl -u <unit> -n 200
```

The apply engine snapshots every rendered file before writing and restores it if
a service reload fails.  Look for `[fail]` and `rolled back` in
`/var/log/eventhub-install.log`.

## AirDrop is not detected / not working

```sh
sudo eventhub airdrop doctor
owly doctor
```

- The installer disables AirDrop if no non-builtin monitor-capable adapter is
  present.  Attach a supported USB adapter and enable it in Modules.
- v0.4.2 has no arm64 release asset; building requires Rust 1.88+.  The
  installer warns and disables AirDrop if `cargo` is missing/old.
- iPhones must use “Everyone for 10 minutes”.  Contacts-only is unsupported.
- Shared mode needs the AP on channel 6, 44 or 149.
- Check the firewall allows TCP 8771 on `awdl0`:
  ```sh
  sudo nft list table inet eventhub
  ```
- If the phone stopped seeing the hub, restart AirDrop/reboot the phone.
- Read [AIRDROP.md](AIRDROP.md) for adapter/driver limitations.

## SMB cannot connect

- Samba users are `eh_<eventhub username>` and must have a passdb entry.  Reset
  the EventHub password or run `sudo eventhub smb sync` (system account only;
  reset via Users to set the SMB password too).
- Check:
  ```sh
  sudo testparm -s /etc/samba/smb.conf
  systemctl status smbd
  ss -ltn | grep 445
  ```
- macOS may cache credentials: connect as `eh_name` and use the EventHub
  password, or remove the keychain entry.
- SMB shooters only get the `Event_<slug>_Ingest` share.  They cannot browse the
  event root by design.

## copyparty login fails / thumbnails missing

- copyparty accounts are argon2 hashes generated with
  `fileserver_copyparty.password_salt`.  If the salt changes, reset passwords.
- Check the service and config:
  ```sh
  systemctl status eventhub-copyparty
  journalctl -u eventhub-copyparty -n 200
  sudo test -r /etc/eventhub/copyparty.conf && echo ok
  ```
- Install `ffmpeg`/Pillow for thumbnails; HEIC/RAW codecs may require extra
  packages.  Thumbnails are best-effort.

## Storage drive missing / not mounted

```sh
sudo eventhub storage status
findmnt /media/eventhub/data
systemctl status 'media-eventhub-data.mount'
journalctl -u 'media-eventhub-data.mount' -n 100
```

- Mounts are by UUID.  If the drive was reformatted, update the UUID in the
  dashboard/wizard.
- Event creation is refused while a configured drive is unmounted, so files
  never land invisibly on the SD card.
- Safe eject:
  ```sh
  sudo eventhub storage unmount
  ```
- Formatting requires the literal token `FORMAT` and a removable drive.

## Hotspot does not start

- Channel/band/country can be illegal for the adapter/regulatory domain.
- WPA3 needs driver/hostapd support; fall back to WPA2.
- `dnsmasq` needs port 53.  If `systemd-resolved` or a distro dnsmasq owns it:
  ```sh
  sudo systemctl disable --now systemd-resolved
  sudo systemctl disable --now dnsmasq
  sudo systemctl restart eventhub-dnsmasq
  ```
  EventHub stops distro hostapd/dnsmasq when applying the hotspot, but the boot
  ordering of `systemd-resolved` can still race.
- `iw reg set <country>` may fail without `wireless-regdb`/CRDA support.

## Wifi uplink does not connect

```sh
sudo eventhub network scan
sudo eventhub network connect "SSID"
sudo eventhub network status --json
sudo eventhub network test
```

`wpa_supplicant`, `dhclient` or `udhcpc` must be installed.  Wifi client mode
needs an interface not used by the AP/AirDrop.

## CPU throttling / undervoltage

The dashboard and `eventhub system status` show the throttle flags.  Use the
official PSU, a short thick cable, and check:
```sh
vcgencmd get_throttled
```
`throttled=0x50000` means it happened in the past; low bits mean it is current.

## Logs and support bundle

```sh
sudo eventhub system logs helper -n 200
sudo eventhub system logs dashboard -n 200
sudo journalctl -b -n 500
sudo tar czf /tmp/eventhub-support.tgz \
  /etc/eventhub /var/log/eventhub /var/lib/eventhub/metrics.json \
  /var/lib/eventhub/network-state.json
```

Remove secrets from the archive before sharing it.
