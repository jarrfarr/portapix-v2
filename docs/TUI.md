# Terminal UI

Run `sudo eventhub` (or `curl … | sudo bash`).  With no flags the TUI is the
single entry point.  It is a **raspi-config-style console menu**: whiptail/dialog
blue dialogs with arrow-key menus over SSH or a serial console.  If whiptail is
not available it falls back to a plain numbered terminal menu.

## Screens

- **First-time setup wizard** — hardware report, interface roles, module
  checklist, storage drive, hotspot SSID/password, admin account, review.
- **Main menu** — Install/Repair, Update, Modules, Configure, Status & logs,
  Backup & restore, Recovery, Uninstall, Quit.
- **Modules** — state (installed/enabled/running/healthy) for every module,
  with enable, disable, start, stop, restart, reinstall and remove.
- **Status & logs** — service status, per-module log text box, self-test.
- **Configure** — interface roles, hotspot settings, storage drive, dashboard
  URL and QR code.
- **Backup & restore** — export config/users/event metadata, restore with
  confirmation.
- **Recovery** — reset admin password, reset hotspot defaults, re-detect
  hardware.
- **Uninstall** — services-only, keep event data, or full wipe.  Full wipe
  requires typing `FULL WIPE` and a second confirmation.

Long-running actions print live install/update output and write to
`/var/log/eventhub-install.log` with `[ok]`, `[warn]` and `[fail]` markers.
Cancel/back is the whiptail Cancel button or menu item 0 in the fallback.

The TUI is a thin presentation layer: it calls the same `TuiController`,
installer, registry and system modules as the CLI and dashboard.  It never
touches system tools directly.

## Non-interactive equivalents

```sh
sudo eventhub install --yes
sudo eventhub install --yes --answers answers.yaml
sudo eventhub update --yes
sudo eventhub uninstall --mode services-only --yes
sudo eventhub uninstall --mode full-wipe --yes --confirm "FULL WIPE"
sudo eventhub module airdrop enable
```

Every TUI action has a CLI equivalent, so unattended reruns are possible.

## Testing

`tests/tui/` uses a scripted fake terminal backend to verify first-boot vs
installed routing, wizard cancellation, main menu exit, module details and QR
encoding.  Image/colour rendering is delegated to whiptail and is not
pixel-tested; the plain fallback is tested directly.
