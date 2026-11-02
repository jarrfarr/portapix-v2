# Terminal UI

Run `sudo eventhub` (or `curl … | sudo bash`).  With no flags the TUI is the
single entry point.  It uses Textual and works over SSH, a serial console, and
at 80×24.

## Screens

- **First-time setup wizard** — hardware report, interface roles, module
  selection, storage drive, hotspot SSID/password, admin account, review.
- **Main menu** — Install/Repair, Update, Modules, Configure, Status & logs,
  Backup & restore, Recovery, Uninstall, Quit.
- **Modules** — state (installed/enabled/running/healthy) for every module,
  with enable, disable, start, stop, restart and reinstall.
- **Status & logs** — health table, per-module journal/log view, self-test.
- **Configure** — interface roles, hotspot settings, save or save & apply.
- **Backup & restore** — export config/users/event metadata to a zip, restore
  with confirmation.
- **Recovery** — reset admin password, reset hotspot defaults, re-detect
  hardware.
- **Uninstall** — services-only, keep event data, or full wipe.  Full wipe
  requires typing `FULL WIPE` and a second confirmation.

Long-running actions run in a Textual worker with a live log pane and a cancel
button.  Output also goes to `/var/log/eventhub-install.log` with `[ok]`,
`[warn]` and `[fail]` markers.  Cancel is cooperative: it takes effect between
installer steps.

## Non-interactive equivalents

```sh
sudo eventhub install --yes
sudo eventhub install --yes --answers answers.yaml
sudo eventhub update --yes
sudo eventhub uninstall --mode services-only --yes
sudo eventhub uninstall --mode full-wipe --yes --confirm "FULL WIPE"
sudo eventhub module airdrop enable
```

Every TUI action has a CLI equivalent, so unattended reruns are possible.  The
TUI contains no business logic; it calls the same installer, registry and
system modules as the CLI and dashboard.
