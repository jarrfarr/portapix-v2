# AirDrop

EventHub receives AirDrop transfers with [owly](https://github.com/t4t5/owly)
(the project formerly known as omdrop-owl).  Owly implements AWDL and AirDrop
for Linux wifi cards whose driver can inject frames.

## Verified upstream

- Repository: <https://github.com/t4t5/owly>
- Pinned default: `v0.4.2`
- License: MIT
- Language: Rust (requires Rust **1.88+**)
- Release assets in v0.4.2 are **x86_64 only**.  Raspberry Pi OS arm64 must
  build from source; the installer does this when `cargo` is new enough.

At install time EventHub clones the pinned ref into `/opt/owly`, runs:

```sh
cargo build --release --locked -p owlyd -p owly-cli
sh packaging/install.sh owly
```

and then uses the installed `owly` and `owlyd` binaries.

## How EventHub wires it

- `owlyd.service` owns the radio.
- `eventhub-radio.service` runs `owly radio up` as root and holds the AWDL
  lease, creating `awdl0`.
- `eventhub-airdrop.service` runs as the unprivileged `eventhub-airdrop` user.
  It resolves the active event and `exec`s:

  ```sh
  owly receive --iface awdl0 --name EventHub \
      --dir <active-event>/ingest/<user> --accept --port 8771
  ```

- nftables allows TCP 8771 only on `awdl0`.
- The target follows the active event; activating an event restarts the
  receiver with the new directory.

## Requirements

- A USB wifi adapter with monitor-mode/injection support, dedicated to AWDL
  (not the AP and not the uplink).
- A kernel with `cfg80211`/`mac80211`, `nl80211`, and the adapter's driver.
  Raspberry Pi OS Bookworm is the expected base; owly is developed against much
  newer kernels, so a kernel/driver update may be required.
- Rust 1.88+ to build on arm64.
- `hostapd` is present for cards that need a hidden AP in exclusive mode.
- NetworkManager should leave the adapter unmanaged; owly's package installs a
  NetworkManager config for this.  If another manager owns the adapter, AWDL
  creation fails; `owly doctor` reports it.

## Adapter compatibility

Compatibility is **per driver and chipset**, not per brand.  Owly maintains a
tested-hardware table in its README and classifies cards as working, unreliable
or failing.  At the time of writing, examples that work in exclusive mode
include MediaTek MT7612U (`mt76x2u`) and several Intel cards (`iwlwifi`), while
many Realtek and older Intel parts fail.  EventHub only checks that a
non-builtin wifi adapter exists with monitor mode; it cannot know whether the
driver will complete an AirDrop transfer.

Run on the Pi:

```sh
eventhub airdrop doctor
# or
owly doctor
```

The installer detects a monitor-capable USB adapter and **disables AirDrop with
a visible warning** when none is found.  It can also disable AirDrop when Rust
is missing/old or the build fails; nothing fails silently.

## Modes and limitations

- **Everyone only.**  Owly's receiver accepts every request in headless mode.
  `airdrop.receive_mode = contacts` is accepted but warned about and falls back
  to Everyone.  iPhones must be set to "Everyone for 10 minutes" (or "Everyone").
- **Shared mode** keeps wifi connected, but the AP must be on channel **6, 44
  or 149**; otherwise the radio refuses to come up.
- **Exclusive / active monitor** modes take wifi down while the radio is held.
  On a Pi with a single wifi adapter this would also take down the hotspot, so
  EventHub requires a dedicated USB adapter.
- **One radio adapter.**  The AP and the AirDrop adapter must be different;
  config validation rejects the same interface for both.
- **No contacts, no Bluetooth proximity prompt guarantee.**  Owly can send BLE
  adverts to wake receivers; EventHub's receiver does not depend on Bluetooth.
- **iPhone quirks.**  If the iPhone stops seeing the hub, restart AirDrop on the
  phone or reboot it; this is a documented owly limitation.
- **Verification.**  Unit tests cover config rendering, unit wiring, target
  selection, adapter detection and installer behaviour with fakes.  An actual
  AirDrop transfer, AWDL link, monitor-mode injection and adapter compatibility
  can only be verified on the physical Pi with an iPhone/Mac.
