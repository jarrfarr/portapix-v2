# Installation

## One command

```sh
curl -fsSL https://raw.githubusercontent.com/OWNER/eventhub/main/bootstrap.sh | sudo bash
```

Replace `OWNER` with the repository owner, or set `EVENTHUB_REPO_URL`:

```sh
curl -fsSL .../bootstrap.sh | sudo EVENTHUB_REPO_URL=https://github.com/you/eventhub bash
```

From a checkout:

```sh
sudo ./bootstrap.sh
```

`bootstrap.sh` installs only git/curl/python3/venv, clones the repository to
`/opt/eventhub/repo`, creates `/opt/eventhub/venv`, installs the package and
starts the terminal UI.  If stdin is piped (`curl | bash`) it reattaches to
`/dev/tty`; if there is no terminal at all it falls back to
`eventhub install --yes`.

## What the installer does

The installer is idempotent.  Running it again repairs an existing install.

1. Detect the distro/package manager and the Pi hardware.
2. Create `/srv/eventhub`, `/var/lib/eventhub`, `/var/log/eventhub`,
   `/etc/eventhub` and the runtime socket directory.
3. Write `/etc/eventhub/config.yaml` mode `0600` with generated secrets.
4. Create unprivileged `eventhub`, `eventhub-web` and `eventhub-airdrop` users.
5. Install system packages (hostapd, dnsmasq, nftables, Samba, Avahi, build
   tools) for the selected modules.
6. Install EventHub into the venv.
7. Create the first administrator and write the password to
   `/var/lib/eventhub/first-boot-admin.txt` mode `0600`.
8. Render all service configuration, snapshot the previous files and apply
   atomically.
9. Install/enable systemd units and start services.
10. Optionally run `eventhub selftest`.

If a critical step fails, the previous configuration file is restored.  A
failed service reload triggers the snapshot rollback in the apply engine.

## Unattended install

```sh
sudo eventhub install --yes --answers answers.yaml
```

Example `answers.yaml`:

```yaml
hostname: shoothub
hotspot:
  ssid: Shoothub
  password: a-long-generated-password
  country_code: GB
storage:
  drive:
    uuid: 1234-ABCD
airdrop:
  enabled: true
  interface: wlx123456
system:
  timezone: Europe/London
```

Only `hotspot.password` is required when the hotspot is enabled.  Everything
else has safe defaults.

## Verification status

Unit tests exercise the installer against fake OS adapters (package manager,
filesystem, systemd, clock).  Package installation, user creation, hostapd,
Samba, AirDrop and the real Raspberry Pi hardware can only be verified on the
physical device.  This is called out in the phase report and in
`docs/TROUBLESHOOTING.md`.
