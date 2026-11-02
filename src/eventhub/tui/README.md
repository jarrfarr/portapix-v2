# tui

raspi-config-style terminal UI, not a full-screen GUI framework.

- `terminal.py` — `WhiptailBackend` (whiptail/dialog menus, inputs, checklists,
  text boxes) and `PlainBackend` numbered-menu fallback.
- `app.py` — `ConsoleTUI`: main menu and every management flow.
- `setup.py` — first-run wizard state and answers patch.
- `service.py` — `TuiController`, the only bridge to installer/registry/system.

The primary UI uses whiptail/dialog, giving the blue Raspberry Pi OS style with
arrow-key navigation and mouse support where the terminal provides it.  If
newt/whiptail is missing, a plain numbered terminal menu works over any SSH or
serial console.

Long actions stream output to the terminal and the install log.  Every action
has a CLI equivalent; the TUI contains no business logic.  Tests use a scripted
fake backend and the plain backend directly.
