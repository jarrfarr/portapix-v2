# installer

Idempotent install/repair, update, uninstall, backup/restore and module
reinstall.

## Steps

1. `DetectSystemStep` — distro/package manager + hardware report.  Warns on
   non-Pi and unsupported adapters; fails only on an unknown package manager
   unless `--skip-packages`.
2. `PrepareFilesystemStep` — data/state/log/config dirs; setgid group-writable
   state/log/data so the dashboard can write sessions/logs.
3. `WriteConfigStep` — validate and write YAML mode `0600` (answers merged).
4. `CreateServiceAccountsStep` — `eventhub`, `eventhub-web` (+ group),
   `eventhub-airdrop`, `eventhub-users`.
5. `InstallPackagesStep` — per-distro package name mapping.
6. `BuildAirdropStep` — installs/builds t4t5/owly; on missing Rust or build
   failure it warns and disables AirDrop.
7. `InstallPythonEnvironmentStep` — venv + pip install.
8. `FirstBootAdminStep` — argon2 + copyparty hash, one-time password file, SMB
   sync, DB group permissions.
9. `RenderUnitsStep` — unit templates + daemon-reload + enable.
10. `ApplyConfigurationStep` — validate/render all service configs.
11. `StartServicesStep` — apply runtime rules, start enabled modules.
12. `SelfTestStep` — optional.

Cancellation is cooperative between steps; a cancelled install restores the
previous config file.  Package installs are not rolled back (safer to leave
them for repair).  Update snapshots config/DB, pulls, reinstalls, applies and
rolls back on failure.  Uninstall modes: services-only, keep event data, or
full wipe (`FULL WIPE` typed confirmation; never formats the drive).

`bootstrap.sh` is the only shell entry point; it installs git/curl/python3/venv,
clones or uses the checkout, creates `/opt/eventhub/venv`, installs the package
and execs the TUI (or non-interactive install with no terminal).

## Testing

`tests/installer/` covers distro detection/package mapping, all installer steps
with fake OS adapters, idempotence, answers files, update checks, uninstall
modes, backup/restore and unit rendering.
