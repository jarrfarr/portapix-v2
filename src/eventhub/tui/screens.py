"""Main TUI screens: menu, modules, status, configure, backup, recovery, uninstall."""

from __future__ import annotations

from typing import Any, ClassVar

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    RadioButton,
    RadioSet,
    RichLog,
    Select,
    Static,
)

from eventhub.tui.common import BaseScreen, ConfirmScreen, ProgressScreen, SummaryScreen
from eventhub.tui.service import OpResult


class MainMenuScreen(BaseScreen):
    BINDINGS: ClassVar[list[Any]] = []
    MENU: ClassVar[list[tuple[str, str, str]]] = [
        ("install", "Install / Repair", "First-time setup or fix a broken install"),
        ("update", "Update", "Pull the latest version, back up and roll back on failure"),
        ("modules", "Modules", "Inspect, enable, disable and reinstall every module"),
        ("configure", "Configure", "Interfaces, hotspot, storage and dashboard"),
        ("status", "Status & logs", "Service health, logs and the self-test"),
        ("backup", "Backup & restore", "Config, users and event metadata"),
        ("recovery", "Recovery", "Reset admin password, hotspot or hardware detection"),
        ("uninstall", "Uninstall", "Remove services, config or everything"),
        ("quit", "Quit", "Leave the TUI"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(id="menu-body"):
            yield Static("EventHub", id="menu-title")
            yield Static(
                "Portable Raspberry Pi photo hub · " + self.hub.controller.dashboard_url(),
                classes="muted",
            )
            yield ListView(
                *[
                    ListItem(Label(f"{label}  ·  {description}"), id=f"menu-{key}")
                    for key, label, description in self.MENU
                ],
                id="main-menu",
            )
            yield Static("", id="menu-status", classes="muted")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_status()

    def _refresh_status(self) -> None:
        try:
            statuses = self.hub.controller.statuses()
            healthy = sum(1 for item in statuses.values() if item.ok)
            self.query_one("#menu-status", Static).update(
                f"{healthy}/{len(statuses)} modules healthy · "
                f"config: {self.hub.controller.config_path}"
            )
        except Exception as exc:
            self.query_one("#menu-status", Static).update(
                f"[yellow]status unavailable: {exc}[/yellow]"
            )

    @on(ListView.Selected, "#main-menu")
    def _selected(self, event: ListView.Selected) -> None:
        key = (event.item.id or "").removeprefix("menu-")
        if key == "quit":
            self.app.exit()
        elif key == "install":
            self._install()
        elif key == "update":
            self._update()
        elif key == "modules":
            self.app.push_screen(ModulesScreen())
        elif key == "configure":
            self.app.push_screen(ConfigureScreen())
        elif key == "status":
            self.app.push_screen(StatusScreen())
        elif key == "backup":
            self.app.push_screen(BackupScreen())
        elif key == "recovery":
            self.app.push_screen(RecoveryScreen())
        elif key == "uninstall":
            self.app.push_screen(UninstallScreen())

    def _install(self) -> None:
        controller = self.hub.controller

        def operation(progress: Any, cancel: Any) -> OpResult:
            return controller.install(progress=progress, cancel=cancel)

        def success(result: OpResult) -> None:
            self.app.push_screen(SummaryScreen(result))

        self.app.push_screen(
            ProgressScreen(
                "Install / repair EventHub",
                operation,
                on_success=success if not controller.installed else None,
            )
        )

    def _update(self) -> None:
        if not self.hub.controller.installed:
            self.hub.notify("EventHub is not installed yet; choose Install / Repair.")
            return
        controller = self.hub.controller

        def operation(progress: Any, cancel: Any) -> OpResult:
            return controller.update(progress=progress)

        self.app.push_screen(ProgressScreen("Update EventHub", operation))


class ModulesScreen(BaseScreen):
    def __init__(self) -> None:
        super().__init__()
        self.selected: str | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(id="screen-body"):
            yield Static("Modules", classes="kicker")
            yield DataTable(id="module-table", cursor_type="row")
            yield Static("", id="module-message")
            with Horizontal(classes="buttons"):
                yield Button("Enable", id="enable", variant="success")
                yield Button("Disable", id="disable", variant="warning")
                yield Button("Start", id="start")
                yield Button("Stop", id="stop")
                yield Button("Restart", id="restart")
                yield Button("Reinstall", id="reinstall", variant="warning")
                yield Button("Refresh", id="refresh")
                yield Button("Back", id="back")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#module-table", DataTable)
        table.add_columns("Module", "Installed", "Enabled", "Running", "Health", "Detail")
        self._refresh()

    def _refresh(self) -> None:
        table = self.query_one("#module-table", DataTable)
        table.clear()
        statuses = self.hub.controller.statuses()
        for name, item in statuses.items():
            table.add_row(
                name,
                "yes" if item.installed else "no",
                "yes" if item.enabled else "no",
                "yes" if item.running else "no",
                item.healthy.value,
                item.detail,
                key=name,
            )
        if table.row_count:
            table.move_cursor(row=0)
            self.selected = next(iter(statuses), None)
        self._message("Select a module, then choose an action.")

    def _message(self, text: str, *, error: bool = False) -> None:
        style = "error" if error else ""
        self.query_one("#module-message", Static).update(
            f"[{style}]{text}[/{style}]" if style else text
        )

    @on(DataTable.RowHighlighted, "#module-table")
    def _highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.selected = str(event.row_key.value)

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        button = event.button.id or ""
        if button == "back":
            self.app.pop_screen()
            return
        if button == "refresh":
            self._refresh()
            return
        if not self.selected:
            self._message("Select a module first.", error=True)
            return
        name = self.selected
        if button in {"enable", "disable"}:
            result = self.hub.controller.toggle_module(name, button == "enable")
        elif button == "reinstall":
            result = self.hub.controller.reinstall_module(name)
        else:
            result = self.hub.controller.module_action(name, button)
        self._message(result.message, error=not result.ok)
        self._refresh()


class StatusScreen(BaseScreen):
    def __init__(self) -> None:
        super().__init__()
        self.selected: str | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(id="screen-body"):
            yield Static("Status and logs", classes="kicker")
            yield DataTable(id="status-table", cursor_type="row")
            yield RichLog(id="status-log", markup=False, wrap=True)
            with Horizontal(classes="buttons"):
                yield Button("Refresh", id="refresh")
                yield Button("Self-test", id="selftest", variant="primary")
                yield Button("Back", id="back")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#status-table", DataTable)
        table.add_columns("Module", "Installed", "Enabled", "Running", "Health", "Detail")
        self._refresh()
        if table.row_count:
            table.move_cursor(row=0)

    def _refresh(self) -> None:
        table = self.query_one("#status-table", DataTable)
        table.clear()
        for name, item in self.hub.controller.statuses().items():
            table.add_row(
                name,
                "yes" if item.installed else "no",
                "yes" if item.enabled else "no",
                "yes" if item.running else "no",
                item.healthy.value,
                item.detail,
                key=name,
            )
        if self.selected:
            self._show_logs(self.selected)

    def _show_logs(self, name: str) -> None:
        self.selected = name
        log = self.query_one("#status-log", RichLog)
        log.clear()
        log.write(f"--- {name} ---")
        log.write(self.hub.controller.logs(name) or "no log output")

    @on(DataTable.RowHighlighted, "#status-table")
    def _highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._show_logs(str(event.row_key.value))

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.app.pop_screen()
        elif event.button.id == "refresh":
            self._refresh()
        elif event.button.id == "selftest":
            controller = self.hub.controller

            def operation(progress: Any, cancel: Any) -> OpResult:
                return controller.selftest(progress=progress)

            self.app.push_screen(ProgressScreen("Self-test", operation))


class ConfigureScreen(BaseScreen):
    ROLES = ("ap", "airdrop", "wan", "lan")

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(id="screen-body"):
            yield Static("Configure", classes="kicker")
            yield Static("Interface roles", classes="field-label")
            options = self._interface_options()
            for role in self.ROLES:
                yield Static(role.upper(), classes="field-label")
                yield Select(
                    options,
                    value=self._role_value(role),
                    allow_blank=False,
                    id=f"cfg-role-{role}",
                )
            yield Static("Hotspot SSID", classes="field-label")
            yield Input(value=self.hub.controller.config.hotspot.ssid, id="cfg-ssid")
            yield Static("Hotspot password", classes="field-label")
            yield Input(
                value=self.hub.controller.config.hotspot.password.get_secret_value(),
                password=True,
                id="cfg-password",
            )
            yield Static("Country code", classes="field-label")
            yield Input(
                value=self.hub.controller.config.hotspot.country_code,
                id="cfg-country",
                max_length=2,
            )
            yield Checkbox(
                "Hide SSID",
                value=self.hub.controller.config.hotspot.hidden,
                id="cfg-hidden",
            )
            yield Static("", id="cfg-message")
            with Horizontal(classes="buttons"):
                yield Button("Save", id="save", variant="primary")
                yield Button("Save & apply", id="apply", variant="success")
                yield Button("Back", id="back")
        yield Footer()

    def _interface_options(self) -> list[tuple[str, str]]:
        try:
            report = self.hub.controller.hardware()
            names = [iface.name for iface in report.interfaces]
        except Exception:
            names = []
        current = [
            str(getattr(self.hub.controller.config.roles, role)) for role in self.ROLES
        ]
        names.extend(value for value in current if value not in {"auto", "none"})
        options = [("Auto-detect", "auto"), ("None / unused", "none")]
        options.extend((name, name) for name in dict.fromkeys(names))
        return options

    def _role_value(self, role: str) -> str:
        return str(getattr(self.hub.controller.config.roles, role))

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        button = event.button.id
        if button == "back":
            self.app.pop_screen()
            return
        patch: dict[str, Any] = {
            "roles": {},
            "hotspot": {
                "ssid": self.query_one("#cfg-ssid", Input).value.strip(),
                "password": self.query_one("#cfg-password", Input).value,
                "country_code": self.query_one("#cfg-country", Input).value.strip().upper(),
                "hidden": self.query_one("#cfg-hidden", Checkbox).value,
            },
        }
        for role in self.ROLES:
            value = self.query_one(f"#cfg-role-{role}", Select).value
            if isinstance(value, str):
                patch["roles"][role] = value
        result = self.hub.controller.save_patch(patch)
        message = self.query_one("#cfg-message", Static)
        if not result.ok:
            message.update(f"[red]{result.message}[/red]")
            return
        if button == "apply":
            applied = self.hub.controller.apply()
            if not applied.ok:
                message.update(f"[red]{applied.message}[/red]")
                return
            message.update("[green]saved and applied[/green]")
        else:
            message.update("[green]saved[/green]")


class BackupScreen(BaseScreen):
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(id="screen-body"):
            yield Static("Backup and restore", classes="kicker")
            yield DataTable(id="backup-table", cursor_type="row")
            yield Static("Restore from path", classes="field-label")
            yield Input(placeholder="/var/lib/eventhub/backups/…", id="restore-path")
            yield Static("", id="backup-message")
            with Horizontal(classes="buttons"):
                yield Button("Export", id="export", variant="primary")
                yield Button("Restore", id="restore", variant="warning")
                yield Button("Refresh", id="refresh")
                yield Button("Back", id="back")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#backup-table", DataTable)
        table.add_columns("Created", "Version", "Size", "Path")
        self._refresh()

    def _refresh(self) -> None:
        table = self.query_one("#backup-table", DataTable)
        table.clear()
        backups = self.hub.controller.backups()
        for info in backups:
            table.add_row(
                info.created_at,
                info.version,
                f"{info.size_bytes / 1024:.0f} KiB",
                str(info.path),
                key=str(info.path),
            )
        if backups:
            self.query_one("#restore-path", Input).value = str(backups[0].path)
        self.query_one("#backup-message", Static).update(
            f"{len(backups)} backup(s)" if backups else "no backups yet"
        )

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        button = event.button.id
        if button == "back":
            self.app.pop_screen()
        elif button == "refresh":
            self._refresh()
        elif button == "export":
            controller = self.hub.controller

            def operation(progress: Any, cancel: Any) -> OpResult:
                return controller.export_backup(progress=progress)

            self.app.push_screen(ProgressScreen("Export backup", operation))
        elif button == "restore":
            path = self.query_one("#restore-path", Input).value.strip()
            if not path:
                self.query_one("#backup-message", Static).update(
                    "[red]Enter a backup path[/red]"
                )
                return

            def confirmed(value: bool | None) -> None:
                if not value:
                    return
                controller = self.hub.controller

                def operation(progress: Any, cancel: Any) -> OpResult:
                    return controller.restore_backup(path, progress=progress)

                self.app.push_screen(
                    ProgressScreen(
                        "Restore backup",
                        operation,
                        on_success=lambda _: self._refresh(),
                    )
                )

            self.app.push_screen(
                ConfirmScreen(
                    f"Restore config and users from:\n{path}\n\nCurrent state will be replaced.",
                    confirm_label="Restore",
                    danger=True,
                ),
                confirmed,
            )


class RecoveryScreen(BaseScreen):
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(id="screen-body"):
            yield Static("Recovery", classes="kicker")
            yield Static("", id="recovery-hardware")
            yield Static("", id="recovery-message")
            with Horizontal(classes="buttons"):
                yield Button("Reset admin password", id="admin", variant="warning")
                yield Button("Reset hotspot defaults", id="hotspot", variant="warning")
                yield Button("Re-detect hardware", id="detect", variant="primary")
                yield Button("Back", id="back")
        yield Footer()

    def on_mount(self) -> None:
        try:
            report = self.hub.controller.hardware()
            self.query_one("#recovery-hardware", Static).update(
                f"Model: {report.model}\n"
                + "\n".join(
                    f"{iface.name}: {iface.kind} {iface.driver}"
                    for iface in report.interfaces
                )
            )
        except Exception as exc:
            self.query_one("#recovery-hardware", Static).update(f"detection failed: {exc}")

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        button = event.button.id
        if button == "back":
            self.app.pop_screen()
            return
        actions = {
            "admin": "reset-admin",
            "hotspot": "reset-hotspot",
            "detect": "detect-hardware",
        }
        action = actions.get(button or "")
        if action is None:
            return
        controller = self.hub.controller

        def operation(progress: Any, cancel: Any) -> OpResult:
            return controller.recovery(action, progress=progress)

        def confirmed(value: bool | None) -> None:
            if value:
                self.app.push_screen(ProgressScreen(f"Recovery: {action}", operation))

        if action == "detect-hardware":
            self.app.push_screen(ProgressScreen(f"Recovery: {action}", operation))
        else:
            self.app.push_screen(
                ConfirmScreen(
                    f"Run recovery action {action!r}?",
                    confirm_label="Run",
                    danger=True,
                ),
                confirmed,
            )


class UninstallScreen(BaseScreen):
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(id="screen-body"):
            yield Static("Uninstall", classes="kicker")
            yield RadioSet(
                RadioButton(
                    "Remove services only (keep data and config)", value=True, id="mode-services"
                ),
                RadioButton("Remove everything except event data", id="mode-keep"),
                RadioButton("Full wipe (remove event data too)", id="mode-full"),
                id="uninstall-mode",
            )
            yield Static(
                "Full wipe requires typing FULL WIPE. The storage drive is never formatted.",
                classes="muted",
            )
            yield Input(placeholder="Type FULL WIPE to confirm a full wipe", id="uninstall-confirm")
            yield Static("", id="uninstall-message")
            with Horizontal(classes="buttons"):
                yield Button("Uninstall", id="uninstall", variant="error")
                yield Button("Back", id="back")
        yield Footer()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.app.pop_screen()
            return
        mode = "services-only"
        pressed = self.query_one("#uninstall-mode", RadioSet).pressed_button
        if pressed is not None:
            mode = {
                "mode-services": "services-only",
                "mode-keep": "keep-data",
                "mode-full": "full-wipe",
            }.get(pressed.id or "", "services-only")
        confirm = self.query_one("#uninstall-confirm", Input).value.strip()
        if mode == "full-wipe" and confirm != "FULL WIPE":
            self.query_one("#uninstall-message", Static).update(
                "[red]Type FULL WIPE exactly to confirm.[/red]"
            )
            return
        controller = self.hub.controller

        def operation(progress: Any, cancel: Any) -> OpResult:
            return controller.uninstall(mode, confirm=confirm, progress=progress)

        def confirmed(value: bool | None) -> None:
            if value:
                self.app.push_screen(ProgressScreen(f"Uninstall ({mode})", operation))

        self.app.push_screen(
            ConfirmScreen(
                f"Uninstall mode: {mode}\n\nThis action cannot be undone.",
                confirm_label="Uninstall",
                danger=True,
            ),
            confirmed,
        )
