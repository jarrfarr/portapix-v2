# airdrop

AirDrop receive through [t4t5/owly](https://github.com/t4t5/owly) (formerly
omdrop-owl).  Owly is a Rust project with two halves: `owlyd` owns the wifi
radio, and `owly receive` accepts transfers on the `awdl0` interface.

## Config (`airdrop:` section)

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Disabled automatically if no monitor-capable USB adapter is detected. |
| `device_name` | `EventHub` | Name shown to iPhones/Macs. |
| `interface` | `auto` | USB wifi adapter used for AWDL. |
| `awdl_interface` | `awdl0` | Interface created by owly. |
| `receive_port` | `8771` | Opened in nftables on `awdl0`. |
| `receive_mode` | `everyone` | `contacts` is accepted but falls back to Everyone (owly has no contacts mode). |
| `target_follows_active_event` | `true` | Received files land in `<active event>/ingest/<user>`. |
| `target_dir` | `/srv/eventhub/inbox` | Fallback when no event is active. |
| `owl_dir` | `/opt/owly` | Source checkout for building. |
| `owl_binary` / `owl_daemon` | `/usr/bin/owly`, `/usr/lib/owly/owlyd` | Installed paths. |
| `owl_repo` / `owl_ref` | t4t5/owly / `v0.4.2` | Pinned upstream. |
| `build_on_install` | `true` | Build when no binaries exist. |
| `allow_untested_hardware` | `false` | Passed to `/etc/owly/radio.toml`. |
| `reconnect_after` | `true` | Owly reconnect-after-monitor behaviour. |

## Systemd units

- `owlyd.service` (installed by owly) — radio daemon.
- `eventhub-radio.service` — `owly radio up`, root, holds the AWDL lease.
- `eventhub-airdrop.service` — `eventhub module airdrop receive`, which resolves
  the active event and `exec`s `owly receive --accept --dir …`.

Event activation re-applies configuration, restarting the receiver with the new
target folder.

## Installer behaviour

`BuildAirdropStep`:

1. Uses existing `owly`/`owlyd` binaries if present.
2. Otherwise requires `cargo` ≥ 1.88 (the Pi release assets are x86_64-only in
   v0.4.2, so arm64 builds from source).
3. Clones `owl_dir` at the pinned ref, runs
   `cargo build --release --locked -p owlyd -p owly-cli`, then
   `packaging/install.sh owly`.
4. On any failure it logs a clear `[warn]`, disables the module in config and
   continues installing the rest of the hub.

## Limitations (see `docs/AIRDROP.md`)

- Adapter compatibility is driver/chipset-specific; `owly doctor` is
  authoritative.
- Everyone mode only; contacts-only is not implemented by owly.
- Shared mode requires the hotspot on channel 6/44/149; exclusive mode takes
  wifi down while the radio is held.
- The receiver needs firewall access to TCP 8771 on `awdl0`.
- Real AirDrop transfers can only be verified with an iPhone/Mac and a
  supported USB adapter on the Pi.
