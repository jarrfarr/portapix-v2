"""raspi-config-style terminal interface for installation and management.

This is intentionally a console menu, not a full-screen Textual application.
whiptail/dialog provides the familiar blue Raspberry Pi OS dialogs; a numbered
plain-text menu is used when newt is unavailable or over a minimal console.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from typing import Any

from eventhub.core.config import ConfigError, generate_wifi_password, merge_config
from eventhub.core.errors import EventHubError
from eventhub.tui.service import OpResult, TuiController
from eventhub.tui.setup import SetupState
from eventhub.tui.terminal import MenuItem, TerminalBackend, make_backend

REQUIRED_MODULES = ("network", "hotspot", "storage", "events", "users", "helper", "dashboard")


def _format_hardware(report: Any) -> str:
    lines = [f"Model: {report.model}", ""]
    if report.interfaces:
        lines.append("Network interfaces:")
        for iface in report.interfaces:
            flags = []
            if iface.supports_ap:
                flags.append("AP")
            if iface.supports_monitor:
                flags.append("monitor")
            suffix = f" ({', '.join(flags)})" if flags else ""
            lines.append(f"  {iface.name}: {iface.kind} {iface.driver or '?'}{suffix}")
    else:
        lines.append("Network interfaces: none detected")
    if report.drives:
        lines.append("")
        lines.append("Storage:")
        for drive in report.drives:
            size = drive.size_bytes // (1024**3)
            lines.append(
                f"  {drive.device.name} {drive.label or drive.uuid} "
                f"{drive.fstype} {size} GiB"
            )
    else:
        lines.append("")
        lines.append("Storage: no removable drive detected")
    if report.warnings:
        lines.extend(["", "Warnings:"] + [f"  - {item}" for item in report.warnings])
    return "\n".join(lines)


def _status_text(statuses: dict[str, Any]) -> str:
    lines = []
    for name, status in statuses.items():
        health = status.healthy.value if hasattr(status.healthy, "value") else status.healthy
        lines.append(
            f"{name:22s} installed={status.installed!s:5s} "
            f"enabled={status.enabled!s:5s} running={status.running!s:5s} "
            f"health={health}\n    {status.detail}"
        )
    return "\n".join(lines)


class ConsoleTUI:
    def __init__(self, controller: TuiController, backend: TerminalBackend) -> None:
        self.controller = controller
        self.backend = backend

    # -- entry ---------------------------------------------------------
    def run(self) -> int:
        if not self.controller.installed:
            if not self._wizard():
                self.backend.msgbox(
                    "Setup not completed",
                    "No changes were made. Run 'sudo eventhub' again when ready.",
                )
                return 1
            if not self.controller.installed:
                return 1
        self._main_menu()
        return 0

    def _main_menu(self) -> None:
        items = [
            MenuItem("install", "Install / Repair"),
            MenuItem("update", "Update"),
            MenuItem("modules", "Modules"),
            MenuItem("configure", "Configure"),
            MenuItem("status", "Status and logs"),
            MenuItem("backup", "Backup and restore"),
            MenuItem("recovery", "Recovery"),
            MenuItem("uninstall", "Uninstall"),
            MenuItem("quit", "Quit"),
        ]
        while True:
            choice = self.backend.menu(
                "EventHub",
                "Portable Raspberry Pi photo hub\n"
                f"Dashboard: {self.controller.dashboard_url()}",
                items,
                default="status",
            )
            if choice in {None, "quit"}:
                return
            try:
                if choice == "install":
                    self._install()
                elif choice == "update":
                    self._update()
                elif choice == "modules":
                    self._modules()
                elif choice == "configure":
                    self._configure()
                elif choice == "status":
                    self._status()
                elif choice == "backup":
                    self._backup()
                elif choice == "recovery":
                    self._recovery()
                elif choice == "uninstall":
                    self._uninstall()
            except EventHubError as exc:
                self.backend.msgbox("Error", str(exc))

    # -- install -------------------------------------------------------
    def _install(self) -> None:
        if self.controller.installed and not self.backend.yesno(
            "Install / Repair",
            "EventHub is already installed. Re-run the installer to repair it?",
            default=True,
        ):
            return
        self._run_install(answers=None)

    def _run_install(self, *, answers: dict[str, Any] | None) -> bool:
        print("\n=== Installing EventHub ===", flush=True)
        result = self.controller.install(
            answers=answers,
            progress=lambda line: print(f"  {line}", flush=True),
        )
        if not result.ok:
            self.backend.msgbox("Installation failed", result.message)
            return False
        self._show_summary(result)
        return True

    def _show_summary(self, result: OpResult) -> None:
        payload: dict[str, Any] = result.payload if isinstance(result.payload, dict) else {}
        config = self.controller.config
        lines = [
            f"Dashboard: {self.controller.dashboard_url()}",
            f"Hotspot:   {config.hotspot.ssid}",
            f"Address:   {config.hotspot.server_ip}",
            "",
            "Join the hotspot and open the dashboard in a browser.",
        ]
        admin_password = payload.get("admin_password")
        if admin_password:
            lines.extend(
                [
                    "",
                    f"Admin user: {payload.get('admin_username', 'admin')}",
                    f"Password:   {admin_password}",
                    "You must change this password on first login.",
                ]
            )
        warnings = payload.get("warnings") or []
        if warnings:
            lines.extend(["", "Warnings:"] + [f"  - {item}" for item in warnings])
        self.backend.msgbox("Setup complete", "\n".join(lines))
        print("\nScan to join the hotspot:\n", flush=True)
        print(self.controller.wifi_qr(), flush=True)
        print(flush=True)

    # -- first-run wizard ---------------------------------------------
    def _wizard(self) -> bool:
        self.backend.msgbox(
            "Welcome to EventHub",
            "This wizard sets up the photo hub.\n\n"
            "It detects hardware, maps interfaces, chooses modules and storage, "
            "creates the hotspot and administrator, then installs.",
        )
        try:
            hardware = self.controller.hardware()
        except Exception as exc:
            hardware = None
            self.backend.msgbox("Hardware detection failed", str(exc))
        if hardware is not None:
            self.backend.msgbox("Hardware report", _format_hardware(hardware))
        state = SetupState()
        state.initialise(self.controller, hardware)

        options = [MenuItem("auto", "Auto-detect"), MenuItem("none", "None / unused")]
        names = [iface.name for iface in hardware.interfaces] if hardware else []
        names.extend(
            value
            for value in state.roles.values()
            if value not in {"auto", "none"} and value not in names
        )
        options.extend(MenuItem(name, name) for name in dict.fromkeys(names))
        role_labels = (
            ("ap", "Hotspot (onboard wifi)"),
            ("airdrop", "AirDrop USB wifi"),
            ("wan", "Internet uplink"),
            ("lan", "Wired editor LAN"),
        )
        for role, label in role_labels:
            choice = self.backend.menu(
                "Interface mapping",
                f"{label}\n\nChoose the interface for role '{role}'.",
                options,
                default=state.roles.get(role, "auto"),
            )
            if choice is None:
                return False
            state.roles[role] = choice

        while True:
            infos = [info for info in self.controller.infos() if info.available]
            checklist = [
                (
                    info.name,
                    f"{info.title} — {info.description}",
                    bool(state.modules.get(info.name, False)),
                )
                for info in infos
            ]
            selected = self.backend.checklist(
                "Modules",
                "Select the modules to install. Space toggles in whiptail.",
                checklist,
            )
            if selected is None:
                return False
            state.modules = {info.name: info.name in selected for info in infos}
            missing = [name for name in REQUIRED_MODULES if name not in selected]
            if missing:
                self.backend.msgbox(
                    "Required modules",
                    "These modules are required: " + ", ".join(missing),
                )
                continue
            break

        drives = hardware.drives if hardware else ()
        if drives:
            drive_items = [MenuItem("auto", "Auto-detect by UUID")]
            drive_items.extend(
                MenuItem(
                    drive.uuid,
                    f"{drive.label or drive.uuid} · {drive.fstype} · "
                    f"{drive.size_bytes // (1024**3)} GiB",
                )
                for drive in drives
                if drive.uuid
            )
            choice = self.backend.menu(
                "Storage",
                "Choose the drive for event data. Existing data is never formatted here.",
                drive_items,
                default="auto",
            )
            if choice is None:
                return False
            state.drive_uuid = choice
        else:
            self.backend.msgbox(
                "Storage",
                "No removable drive was detected. Event data will use the fallback "
                "path; you can configure a drive later in Configure.",
            )

        while True:
            ssid = self.backend.input(
                "Hotspot",
                "SSID for the hub's wifi network:",
                default=state.ssid,
            )
            if ssid is None:
                return False
            password = self.backend.input(
                "Hotspot password",
                "WPA2/WPA3 password. Leave blank to generate a strong one:",
                password=True,
            )
            if password is None:
                return False
            if not password:
                password = generate_wifi_password()
            country = self.backend.input(
                "Country code",
                "Two-letter regulatory country code (for example GB or US):",
                default=state.country,
            )
            if country is None:
                return False
            state.ssid = ssid.strip()
            state.password = password
            state.country = country.strip().upper()
            try:
                merge_config(
                    self.controller.config,
                    {
                        "roles": state.roles,
                        "hotspot": {
                            "ssid": state.ssid,
                            "password": state.password,
                            "country_code": state.country,
                            "interface": state.roles["ap"],
                        },
                    },
                )
            except ConfigError as exc:
                self.backend.msgbox("Invalid hotspot settings", str(exc))
                continue
            break

        while True:
            username = self.backend.input(
                "Administrator",
                "Dashboard administrator username:",
                default=state.admin_username,
            )
            if username is None:
                return False
            username = username.strip().lower()
            password = self.backend.input(
                "Administrator password",
                "Password (blank generates one):",
                password=True,
            )
            if password is None:
                return False
            if not password:
                password = generate_wifi_password()
            confirm = self.backend.input(
                "Administrator password",
                "Repeat the password (blank is accepted for a generated one):",
                password=True,
            )
            if confirm is None:
                return False
            if confirm and confirm != password:
                self.backend.msgbox("Passwords do not match", "Please try again.")
                continue
            minimum = self.controller.config.users.min_password_length
            if len(password) < minimum:
                self.backend.msgbox(
                    "Password too short",
                    f"Use at least {minimum} characters.",
                )
                continue
            state.admin_username = username
            state.admin_password = password
            break

        selected_modules = ", ".join(
            name for name, enabled in state.modules.items() if enabled
        )
        review = [
            f"Hotspot: {state.ssid} on {state.roles['ap']} ({state.country})",
            f"Roles: {state.roles}",
            f"Modules: {selected_modules}",
            f"Storage UUID: {state.drive_uuid}",
            f"Administrator: {state.admin_username}",
            "",
            "Install now?",
        ]
        if not self.backend.yesno("Review", "\n".join(review), default=True):
            return False
        answers = state.build_patch(self.controller)
        return self._run_wizard_install(answers, state)

    def _run_wizard_install(self, answers: dict[str, Any], state: SetupState) -> bool:
        print("\n=== Installing EventHub ===", flush=True)
        result = self.controller.install(
            answers=answers,
            progress=lambda line: print(f"  {line}", flush=True),
            admin_username=state.admin_username,
            admin_password=state.admin_password,
        )
        if not result.ok:
            self.backend.msgbox("Installation failed", result.message)
            return False
        self._show_summary(result)
        return True

    # -- update --------------------------------------------------------
    def _update(self) -> None:
        if not self.controller.installed:
            self.backend.msgbox("Update", "EventHub is not installed yet.")
            return
        if not self.backend.yesno(
            "Update",
            "Back up config/users, pull the latest version and restart services?",
            default=False,
        ):
            return
        print("\n=== Updating EventHub ===", flush=True)
        result = self.controller.update(progress=lambda line: print(f"  {line}", flush=True))
        self.backend.msgbox("Update", result.message)

    # -- modules -------------------------------------------------------
    def _modules(self) -> None:
        while True:
            statuses = self.controller.statuses()
            items = []
            for name, status in statuses.items():
                health = (
                    status.healthy.value
                    if hasattr(status.healthy, "value")
                    else status.healthy
                )
                items.append(
                    MenuItem(
                        name,
                        f"{name} · {'enabled' if status.enabled else 'disabled'} · "
                        f"{'running' if status.running else 'stopped'} · {health}",
                    )
                )
            choice = self.backend.menu(
                "Modules",
                "Select a module to manage.",
                items,
            )
            if choice is None:
                return
            status = statuses[choice]
            actions = [
                MenuItem("enable", "Enable"),
                MenuItem("disable", "Disable"),
                MenuItem("start", "Start"),
                MenuItem("stop", "Stop"),
                MenuItem("restart", "Restart"),
                MenuItem("reinstall", "Reinstall"),
                MenuItem("remove", "Remove service (keep packages/data)"),
                MenuItem("details", "Show details"),
            ]
            action = self.backend.menu(
                f"Module: {choice}",
                f"State: installed={status.installed} enabled={status.enabled} "
                f"running={status.running}\n{status.detail}",
                actions,
            )
            if action is None:
                continue
            if action == "details":
                self.backend.textbox(f"{choice} details", status.detail or "no details")
                continue
            if action in {"reinstall", "remove"} and not self.backend.yesno(
                action.title(),
                f"Really {action} {choice}?",
                default=False,
            ):
                continue
            if action == "enable":
                result = self.controller.toggle_module(choice, True)
            elif action == "disable":
                result = self.controller.toggle_module(choice, False)
            elif action == "reinstall":
                result = self.controller.reinstall_module(choice)
            elif action == "remove":
                result = self.controller.remove_module(choice)
            else:
                result = self.controller.module_action(choice, action)
            self.backend.msgbox(f"Module: {choice}", result.message)

    # -- configure -----------------------------------------------------
    def _configure(self) -> None:
        while True:
            choice = self.backend.menu(
                "Configure",
                "Choose what to change. Changes are saved and can be applied immediately.",
                [
                    MenuItem("roles", "Interface roles"),
                    MenuItem("hotspot", "Hotspot settings"),
                    MenuItem("storage", "Storage drive"),
                    MenuItem("dashboard", "Dashboard address and QR code"),
                    MenuItem("back", "Back"),
                ],
            )
            if choice in {None, "back"}:
                return
            if choice == "roles":
                self._configure_roles()
            elif choice == "hotspot":
                self._configure_hotspot()
            elif choice == "storage":
                self._configure_storage()
            elif choice == "dashboard":
                self.backend.msgbox("Dashboard", self.controller.dashboard_url())
                print("\nDashboard QR code:\n", flush=True)
                print(self.controller.dashboard_qr(), flush=True)

    def _save_and_apply(self, patch: dict[str, Any]) -> None:
        result = self.controller.save_patch(patch)
        if not result.ok:
            self.backend.msgbox("Save failed", result.message)
            return
        if self.backend.yesno(
            "Apply configuration",
            "Configuration saved. Apply it and restart affected services now?",
            default=True,
        ):
            applied = self.controller.apply()
            self.backend.msgbox(
                "Apply", applied.message if hasattr(applied, "message") else str(applied)
            )

    def _configure_roles(self) -> None:
        hardware = None
        with contextlib.suppress(Exception):
            hardware = self.controller.hardware()
        names = [iface.name for iface in hardware.interfaces] if hardware else []
        config = self.controller.config
        names.extend(
            value
            for value in (
                config.roles.ap,
                config.roles.airdrop,
                config.roles.wan,
                config.roles.lan,
            )
            if value not in {"auto", "none"} and value not in names
        )
        options = [MenuItem("auto", "Auto-detect"), MenuItem("none", "None / unused")]
        options.extend(MenuItem(name, name) for name in dict.fromkeys(names))
        roles: dict[str, str] = {}
        for role, label in (
            ("ap", "Hotspot (onboard wifi)"),
            ("airdrop", "AirDrop USB wifi"),
            ("wan", "Internet uplink"),
            ("lan", "Wired editor LAN"),
        ):
            current = getattr(config.roles, role)
            choice = self.backend.menu(
                f"Role: {label}",
                f"Assign an interface to '{role}'.",
                options,
                default=current,
            )
            if choice is None:
                return
            roles[role] = choice
        self._save_and_apply(
            {
                "roles": roles,
                "hotspot": {"interface": roles["ap"]},
                "airdrop": {"interface": roles["airdrop"]},
            }
        )

    def _configure_hotspot(self) -> None:
        h = self.controller.config.hotspot
        ssid = self.backend.input("Hotspot SSID", "Network name:", default=h.ssid)
        if ssid is None:
            return
        password = self.backend.input(
            "Hotspot password",
            "New WPA2/WPA3 password. Leave blank to keep the current one:",
            password=True,
        )
        if password is None:
            return
        country = self.backend.input(
            "Country code",
            "Two-letter regulatory country code:",
            default=h.country_code,
        )
        if country is None:
            return
        band = self.backend.menu(
            "Hotspot band",
            "Choose the wifi band.",
            [
                MenuItem("auto", "Auto"),
                MenuItem("2.4", "2.4 GHz"),
                MenuItem("5", "5 GHz"),
            ],
            default=h.band,
        )
        if band is None:
            return
        channel_text = self.backend.input(
            "Channel",
            "Channel number (0 = automatic):",
            default=str(h.channel),
        )
        if channel_text is None:
            return
        try:
            channel = int(channel_text or "0")
        except ValueError:
            self.backend.msgbox("Invalid channel", "Channel must be a number.")
            return
        patch = {
            "hotspot": {
                "ssid": ssid.strip(),
                "country_code": country.strip().upper(),
                "band": band,
                "channel": channel,
            }
        }
        if password:
            patch["hotspot"]["password"] = password
        self._save_and_apply(patch)

    def _configure_storage(self) -> None:
        hardware = None
        with contextlib.suppress(Exception):
            hardware = self.controller.hardware()
        items = [MenuItem("auto", "Auto-detect by UUID")]
        if hardware:
            items.extend(
                MenuItem(
                    drive.uuid,
                    f"{drive.label or drive.uuid} · {drive.fstype} · "
                    f"{drive.size_bytes // (1024**3)} GiB",
                )
                for drive in hardware.drives
                if drive.uuid
            )
        items.extend(
            [
                MenuItem("manual", "Enter UUID manually"),
                MenuItem("back", "Back"),
            ]
        )
        choice = self.backend.menu(
            "Storage drive",
            "Choose the drive for events. Existing data is never formatted here.",
            items,
        )
        if choice in {None, "back"}:
            return
        uuid = ""
        if choice == "manual":
            uuid = self.backend.input("Storage UUID", "UUID of the filesystem:") or ""
        elif choice != "auto":
            uuid = choice
        self._save_and_apply({"storage": {"drive": {"uuid": uuid}}})

    # -- status/logs ---------------------------------------------------
    def _status(self) -> None:
        while True:
            choice = self.backend.menu(
                "Status and logs",
                "Inspect service health, run the self-test or view logs.",
                [
                    MenuItem("status", "Service status"),
                    MenuItem("selftest", "Run self-test"),
                    MenuItem("logs", "View logs"),
                    MenuItem("back", "Back"),
                ],
            )
            if choice in {None, "back"}:
                return
            if choice == "status":
                self.backend.textbox(
                    "Service status", _status_text(self.controller.statuses())
                )
            elif choice == "selftest":
                print("\n=== Self-test ===", flush=True)
                result = self.controller.selftest(
                    progress=lambda line: print(f"  {line}", flush=True)
                )
                report = result.payload
                if report is not None and hasattr(report, "checks"):
                    lines = []
                    for check in report.checks:
                        state = "SKIP" if check.skipped else "PASS" if check.ok else "FAIL"
                        lines.append(f"[{state}] {check.name}: {check.detail}")
                    self.backend.msgbox("Self-test", result.message)
                    self.backend.textbox("Self-test details", "\n".join(lines))
                else:
                    self.backend.msgbox("Self-test", result.message)
            elif choice == "logs":
                statuses = self.controller.statuses()
                module = self.backend.menu(
                    "Logs",
                    "Choose a module.",
                    [MenuItem(name, name) for name in statuses],
                )
                if module:
                    self.backend.textbox(
                        f"{module} logs",
                        self.controller.logs(module, lines=300) or "no log output",
                    )

    # -- backup --------------------------------------------------------
    def _backup(self) -> None:
        while True:
            choice = self.backend.menu(
                "Backup and restore",
                "Back up config, users and event metadata, or restore a backup.",
                [
                    MenuItem("export", "Export backup"),
                    MenuItem("restore", "Restore backup"),
                    MenuItem("back", "Back"),
                ],
            )
            if choice in {None, "back"}:
                return
            if choice == "export":
                print("\n=== Export backup ===", flush=True)
                result = self.controller.export_backup(
                    progress=lambda line: print(f"  {line}", flush=True)
                )
                self.backend.msgbox("Export backup", result.message)
            else:
                path = self.backend.input(
                    "Restore backup",
                    "Path to the backup zip:",
                )
                if not path:
                    continue
                if self.backend.yesno(
                    "Restore backup",
                    "Current config and users will be replaced. Continue?",
                    default=False,
                ):
                    print("\n=== Restore backup ===", flush=True)
                    result = self.controller.restore_backup(
                        path, progress=lambda line: print(f"  {line}", flush=True)
                    )
                    self.backend.msgbox("Restore backup", result.message)

    # -- recovery ------------------------------------------------------
    def _recovery(self) -> None:
        actions = {
            "admin": "reset-admin",
            "hotspot": "reset-hotspot",
            "detect": "detect-hardware",
        }
        choice = self.backend.menu(
            "Recovery",
            "Use these if the hub is locked or misconfigured.",
            [
                MenuItem("admin", "Reset admin password"),
                MenuItem("hotspot", "Reset hotspot to EventHub-Recovery"),
                MenuItem("detect", "Re-detect hardware"),
                MenuItem("back", "Back"),
            ],
        )
        if choice in {None, "back"}:
            return
        if choice in {"admin", "hotspot"} and not self.backend.yesno(
            "Recovery",
            f"Run {actions[choice]}? This changes live credentials.",
            default=False,
        ):
            return
        print("\n=== Recovery ===", flush=True)
        result = self.controller.recovery(
            actions[choice], progress=lambda line: print(f"  {line}", flush=True)
        )
        payload = result.payload if isinstance(result.payload, dict) else {}
        message = result.message
        if payload.get("password"):
            message += f"\n\nNew password: {payload['password']}"
            if payload.get("file"):
                message += f"\nWritten to: {payload['file']}"
        self.backend.msgbox("Recovery", message)

    # -- uninstall -----------------------------------------------------
    def _uninstall(self) -> None:
        mode = self.backend.menu(
            "Uninstall",
            "Choose what to remove. Event data is only removed by full wipe.",
            [
                MenuItem("services-only", "Remove services only (keep data/config)"),
                MenuItem("keep-data", "Remove everything except event data"),
                MenuItem("full-wipe", "Full wipe (remove event data too)"),
                MenuItem("back", "Back"),
            ],
        )
        if mode in {None, "back"}:
            return
        confirm = ""
        if mode == "full-wipe":
            confirm = self.backend.input(
                "Full wipe",
                "Type FULL WIPE exactly to confirm deleting event data:",
            )
            if confirm != "FULL WIPE":
                self.backend.msgbox("Full wipe", "Confirmation did not match.")
                return
        if not self.backend.yesno(
            "Uninstall",
            f"Run uninstall ({mode})? This cannot be undone.",
            default=False,
        ):
            return
        print("\n=== Uninstalling EventHub ===", flush=True)
        result = self.controller.uninstall(
            mode, confirm=confirm, progress=lambda line: print(f"  {line}", flush=True)
        )
        self.backend.msgbox("Uninstall", result.message)


def run_tui(
    *,
    config: Any | None,
    config_path: Path | str,
    dry_run: bool = False,
    runtime: Any | None = None,
    backend: TerminalBackend | None = None,
) -> int:
    controller = TuiController(config_path, dry_run=dry_run, runtime=runtime)
    if config is not None:
        controller.runtime.config = config
    ui = ConsoleTUI(controller, backend or make_backend())
    try:
        return ui.run()
    finally:
        with contextlib.suppress(Exception):
            controller.runtime.close()


def main() -> None:
    sys.exit(run_tui(config=None, config_path=Path("/etc/eventhub/config.yaml")))


if __name__ == "__main__":
    main()
