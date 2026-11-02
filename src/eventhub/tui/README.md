# tui

Textual terminal UI: first-run wizard and management menu.

- `service.py` — `TuiController`, the only bridge to installer/registry/system.
- `wizard.py` — hardware report, interface roles, modules, storage, hotspot,
  admin, review, then install.
- `screens.py` — main menu, modules, status/logs, configure, backup, recovery,
  uninstall.
- `common.py` — progress worker with live log/cancel, confirmation modal,
  summary with credentials and wifi QR.
- `app.py` — Textual app, CSS, menu reset, headless-testable entry.

The TUI has no business logic.  Long actions run in a worker; cancellation is
observed between installer steps.  Every action has a CLI equivalent, so the
TUI can be bypassed for automation.  It renders at 80×24 and works over SSH or
a serial console.

Tests use Textual's pilot harness to verify first-boot vs installed routing,
wizard navigation, module/status/config pages, backup/recovery/uninstall
rendering and QR encoding.
