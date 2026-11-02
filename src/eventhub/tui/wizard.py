"""First-time setup wizard screens (hardware → interfaces → modules → …)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Checkbox, Footer, Header, Input, Select, Static

from eventhub.core.config import ConfigError, generate_wifi_password, merge_config
from eventhub.network.hardware import HardwareReport
from eventhub.tui.common import BaseScreen, ProgressScreen, SummaryScreen
from eventhub.tui.service import OpResult

REQUIRED_MODULES = {
    "network",
    "hotspot",
    "storage",
    "events",
    "users",
    "helper",
    "dashboard",
}


@dataclass(slots=True)
class WizardState:
    hardware: HardwareReport | None = None
    roles: dict[str, str] = field(
        default_factory=lambda: {"ap": "auto", "airdrop": "auto", "wan": "auto", "lan": "auto"}
    )
    modules: dict[str, bool] = field(default_factory=dict)
    drive_uuid: str = "auto"
    ssid: str = "EventHub"
    password: str = ""
    country: str = "GB"
    hidden: bool = False
    admin_username: str = "admin"
    admin_password: str = ""

    def initialise(self, controller: Any) -> None:
        config = controller.config
        self.ssid = config.hotspot.ssid
        self.password = config.hotspot.password.get_secret_value()
        self.country = config.hotspot.country_code
        self.hidden = config.hotspot.hidden
        self.roles = {
            "ap": config.roles.ap,
            "airdrop": config.roles.airdrop,
            "wan": config.roles.wan,
            "lan": config.roles.lan,
        }
        for info in controller.infos():
            self.modules[info.name] = (
                config.module_enabled(info.name) if info.available else False
            )
        if self.hardware is not None and not self.hardware.airdrop_supported:
            self.modules["airdrop"] = False

    def build_patch(self, controller: Any) -> dict[str, Any]:
        patch: dict[str, Any] = {
            "roles": dict(self.roles),
            "hotspot": {
                "ssid": self.ssid,
                "password": self.password,
                "country_code": self.country,
                "hidden": self.hidden,
                "interface": self.roles["ap"],
            },
            "airdrop": {
                "enabled": bool(self.modules.get("airdrop", False)),
                "interface": self.roles["airdrop"],
            },
        }
        for info in controller.infos():
            if not info.available:
                continue
            section = patch.setdefault(info.config_section, {})
            section["enabled"] = bool(self.modules.get(info.name, False))
        if self.drive_uuid and self.drive_uuid != "auto":
            patch.setdefault("storage", {})["drive"] = {"uuid": self.drive_uuid}
        return patch


class WizardMixin:
    hub: Any
    state: WizardState

    def wizard(self) -> WizardState:
        return self.hub.wizard


class WizardHardwareScreen(WizardMixin, BaseScreen):
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(id="wizard-body"):
            yield Static("First-time setup · 1/6 · Hardware", classes="kicker")
            yield Static(
                "Detecting Raspberry Pi, network adapters and storage…",
                id="hardware-report",
            )
        with Horizontal(classes="buttons"):
            yield Button("Next", id="next", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        state = self.wizard()
        try:
            state.hardware = self.hub.controller.hardware()
        except Exception as exc:
            self.query_one("#hardware-report", Static).update(
                f"[yellow]Hardware detection failed: {exc}[/yellow]"
            )
            return
        state.initialise(self.hub.controller)
        report = state.hardware
        lines = [
            f"Model: [bold]{report.model}[/bold]"
            f"{' (Raspberry Pi)' if report.is_raspberry_pi else ' (not a Pi)'}",
            "",
            "Network interfaces:",
        ]
        if report.interfaces:
            for iface in report.interfaces:
                labels = []
                if iface.supports_ap:
                    labels.append("AP")
                if iface.supports_monitor:
                    labels.append("monitor")
                label = f" ({', '.join(labels)})" if labels else ""
                lines.append(
                    f"  {iface.name}: {iface.kind} {iface.driver or '?'}"
                    f"{label} · {iface.state}"
                )
        else:
            lines.append("  none detected")
        lines.append("")
        lines.append("Storage:")
        if report.drives:
            for drive in report.drives:
                lines.append(
                    f"  /dev/{drive.device.name} {drive.label or drive.uuid} "
                    f"{drive.fstype} {drive.size_bytes // (1024**3)} GiB"
                )
        else:
            lines.append("  no removable drive detected")
        lines.append("")
        lines.append("Suggested roles: " + ", ".join(
            f"{role}={name}" for role, name in report.suggested_roles.items()
        ))
        if report.warnings:
            lines.append("")
            lines.append("[yellow]Warnings:[/yellow]")
            lines.extend(f"  - {warning}" for warning in report.warnings)
        self.query_one("#hardware-report", Static).update("\n".join(lines))

    @on(Button.Pressed, "#next")
    def _next(self) -> None:
        self.app.push_screen(WizardInterfacesScreen())


class WizardInterfacesScreen(WizardMixin, BaseScreen):
    def _options(self) -> list[tuple[str, str]]:
        report = self.wizard().hardware
        names = [iface.name for iface in report.interfaces] if report else []
        names.extend(
            value
            for value in self.wizard().roles.values()
            if value not in {"auto", "none"}
        )
        options = [("Auto-detect", "auto"), ("None / unused", "none")]
        options.extend((name, name) for name in dict.fromkeys(names))
        return options

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(id="wizard-body"):
            yield Static("First-time setup · 2/6 · Interface roles", classes="kicker")
            yield Static(
                "Map each role to a stable interface name. Never hardcoded.",
                classes="muted",
            )
            for role, label in (
                ("ap", "Hotspot (onboard wifi)"),
                ("airdrop", "AirDrop USB wifi"),
                ("wan", "Internet uplink"),
                ("lan", "Wired editor LAN"),
            ):
                yield Static(label, classes="field-label")
                options = self._options()
                yield Select(
                    options,
                    value=self.wizard().roles.get(role, "auto"),
                    allow_blank=False,
                    id=f"role-{role}",
                )
            yield Static("", id="wizard-error", classes="error")
            with Horizontal(classes="buttons"):
                yield Button("Back", id="back")
                yield Button("Next", id="next", variant="primary")
        yield Footer()

    @on(Button.Pressed, "#back")
    def _back(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed, "#next")
    def _next(self) -> None:
        state = self.wizard()
        for role in ("ap", "airdrop", "wan", "lan"):
            select = self.query_one(f"#role-{role}", Select)
            value = select.value
            if isinstance(value, str):
                state.roles[role] = value
        if (
            state.roles["ap"] not in {"auto", "none"}
            and state.roles["ap"] == state.roles["airdrop"]
        ):
            self.query_one("#wizard-error", Static).update(
                "The AP and AirDrop interfaces must be different."
            )
            return
        self.app.push_screen(WizardModulesScreen())


class WizardModulesScreen(WizardMixin, BaseScreen):
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(id="wizard-body"):
            yield Static("First-time setup · 3/6 · Modules", classes="kicker")
            yield Static(
                "Core modules are required. Disable AirDrop if its adapter is unsupported.",
                classes="muted",
            )
            for info in self.hub.controller.infos():
                if info.available:
                    yield Checkbox(
                        f"{info.title} — {info.description}",
                        value=bool(self.wizard().modules.get(info.name, False)),
                        id=f"module-{info.name}",
                    )
                else:
                    yield Static(f"[dim]{info.title}: not available in this build[/dim]")
            yield Static("", id="wizard-error", classes="error")
            with Horizontal(classes="buttons"):
                yield Button("Back", id="back")
                yield Button("Next", id="next", variant="primary")
        yield Footer()

    @on(Button.Pressed, "#back")
    def _back(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed, "#next")
    def _next(self) -> None:
        state = self.wizard()
        for info in self.hub.controller.infos():
            if not info.available:
                continue
            checkbox = self.query_one(f"#module-{info.name}", Checkbox)
            state.modules[info.name] = checkbox.value
        missing = [name for name in REQUIRED_MODULES if not state.modules.get(name)]
        if missing:
            self.query_one("#wizard-error", Static).update(
                "These modules are required: " + ", ".join(sorted(missing))
            )
            return
        self.app.push_screen(WizardStorageScreen())


class WizardStorageScreen(WizardMixin, BaseScreen):
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(id="wizard-body"):
            yield Static("First-time setup · 4/6 · Storage", classes="kicker")
            report = self.wizard().hardware
            if report and report.drives:
                options = [("Auto-detect by UUID", "auto")]
                for drive in report.drives:
                    label = drive.label or drive.uuid or drive.device.name
                    options.append(
                        (
                            f"{label} · {drive.fstype} · "
                            f"{drive.size_bytes // (1024**3)} GiB · {drive.uuid}",
                            drive.uuid,
                        )
                    )
                yield Select(
                    options,
                    value=self.wizard().drive_uuid,
                    allow_blank=False,
                    id="drive",
                )
            else:
                yield Static(
                    "[yellow]No removable drive was detected. Plug in the SSD/HDD and "
                    "re-run detection, or configure it later in Configure.[/yellow]"
                )
            yield Static(
                "Existing data is never formatted or erased by the wizard.",
                classes="muted",
            )
            with Horizontal(classes="buttons"):
                yield Button("Back", id="back")
                yield Button("Next", id="next", variant="primary")
        yield Footer()

    @on(Button.Pressed, "#back")
    def _back(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed, "#next")
    def _next(self) -> None:
        select = self.query_selector("#drive")
        if select is not None:
            value = self.query_one("#drive", Select).value
            if isinstance(value, str):
                self.wizard().drive_uuid = value
        self.app.push_screen(WizardHotspotScreen())


class WizardHotspotScreen(WizardMixin, BaseScreen):
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(id="wizard-body"):
            yield Static("First-time setup · 5/6 · Hotspot", classes="kicker")
            yield Static("SSID", classes="field-label")
            yield Input(value=self.wizard().ssid, id="ssid", max_length=32)
            yield Static("WPA2/WPA3 password", classes="field-label")
            with Horizontal():
                yield Input(
                    value=self.wizard().password,
                    password=True,
                    id="password",
                    max_length=63,
                )
                yield Button("Generate", id="generate")
            yield Static("Country code (2 letters)", classes="field-label")
            yield Input(value=self.wizard().country, id="country", max_length=2)
            yield Checkbox("Hide the SSID", value=self.wizard().hidden, id="hidden")
            yield Static("", id="wizard-error", classes="error")
            with Horizontal(classes="buttons"):
                yield Button("Back", id="back")
                yield Button("Next", id="next", variant="primary")
        yield Footer()

    @on(Button.Pressed, "#generate")
    def _generate(self) -> None:
        self.query_one("#password", Input).value = generate_wifi_password()

    @on(Button.Pressed, "#back")
    def _back(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed, "#next")
    def _next(self) -> None:
        state = self.wizard()
        state.ssid = self.query_one("#ssid", Input).value.strip()
        state.password = self.query_one("#password", Input).value
        state.country = self.query_one("#country", Input).value.strip().upper()
        state.hidden = self.query_one("#hidden", Checkbox).value
        try:
            merge_config(
                self.hub.controller.config,
                {
                    "hotspot": {
                        "ssid": state.ssid,
                        "password": state.password,
                        "country_code": state.country,
                        "hidden": state.hidden,
                        "interface": state.roles["ap"],
                    }
                },
            )
        except ConfigError as exc:
            self.query_one("#wizard-error", Static).update(str(exc))
            return
        self.app.push_screen(WizardAdminScreen())


class WizardAdminScreen(WizardMixin, BaseScreen):
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(id="wizard-body"):
            yield Static("First-time setup · 6/6 · Administrator", classes="kicker")
            yield Static("Username", classes="field-label")
            yield Input(value=self.wizard().admin_username, id="admin-user", max_length=32)
            yield Static("Password (leave blank to generate)", classes="field-label")
            yield Input(password=True, id="admin-password", max_length=128)
            yield Static("Confirm password", classes="field-label")
            yield Input(password=True, id="admin-confirm", max_length=128)
            yield Static("", id="wizard-error", classes="error")
            with Horizontal(classes="buttons"):
                yield Button("Back", id="back")
                yield Button("Review", id="next", variant="primary")
        yield Footer()

    @on(Button.Pressed, "#back")
    def _back(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed, "#next")
    def _next(self) -> None:
        state = self.wizard()
        username = self.query_one("#admin-user", Input).value.strip().lower()
        minimum = self.hub.controller.config.users.min_password_length
        password = self.query_one("#admin-password", Input).value
        confirm = self.query_one("#admin-confirm", Input).value
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{2,31}", username):
            self.query_one("#wizard-error", Static).update(
                "Username must be 3-32 characters: lowercase letters, digits, _ or -."
            )
            return
        if not password:
            password = generate_wifi_password()
        if len(password) < minimum:
            self.query_one("#wizard-error", Static).update(
                f"Password must be at least {minimum} characters."
            )
            return
        if password != confirm and confirm:
            self.query_one("#wizard-error", Static).update("Passwords do not match.")
            return
        state.admin_username = username
        state.admin_password = password
        self.app.push_screen(WizardReviewScreen())


class WizardReviewScreen(WizardMixin, BaseScreen):
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(id="wizard-body"):
            yield Static("First-time setup · Review", classes="kicker")
            yield Static("", id="review-text")
            yield Static(
                "Nothing has been changed yet. Finishing will install packages, write "
                "configuration, render service configs and start services.",
                classes="muted",
            )
            yield Static("", id="wizard-error", classes="error")
            with Horizontal(classes="buttons"):
                yield Button("Back", id="back")
                yield Button("Install EventHub", id="finish", variant="success")
        yield Footer()

    def on_mount(self) -> None:
        state = self.wizard()
        modules = ", ".join(name for name, enabled in state.modules.items() if enabled)
        lines = [
            f"Hotspot: [bold]{state.ssid}[/bold] on {state.roles['ap']}",
            f"Country: {state.country} · hidden={state.hidden}",
            f"Roles: {state.roles}",
            f"Modules: {modules}",
            f"Storage UUID: {state.drive_uuid}",
            f"Administrator: {state.admin_username}",
        ]
        self.query_one("#review-text", Static).update("\n".join(lines))

    @on(Button.Pressed, "#back")
    def _back(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed, "#finish")
    def _finish(self) -> None:
        state = self.wizard()
        patch = state.build_patch(self.hub.controller)
        controller = self.hub.controller

        def operation(progress: Any, cancel: Any) -> OpResult:
            result = controller.install(
                answers=patch, progress=progress, cancel=cancel
            )
            return result

        def success(result: OpResult) -> None:
            self.app.push_screen(SummaryScreen(result))

        self.app.push_screen(
            ProgressScreen("Installing EventHub", operation, on_success=success)
        )
