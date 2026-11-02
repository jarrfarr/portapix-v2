"""Shared TUI screens: base, confirmation, long-running progress and summary."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar, cast

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Header, LoadingIndicator, RichLog, Static

from eventhub.tui.service import OpResult, Progress

if TYPE_CHECKING:
    from eventhub.tui.app import EventHubApp

Operation = Callable[[Progress, threading.Event], OpResult]


class BaseScreen(Screen[None]):
    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "back", "Back")]

    @property
    def hub(self) -> EventHubApp:
        return cast("EventHubApp", self.app)

    def action_back(self) -> None:
        if len(self.app.screen_stack) > 2:
            self.app.pop_screen()
        else:
            self.app.exit()


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self,
        message: str,
        *,
        title: str = "Please confirm",
        confirm_label: str = "Confirm",
        danger: bool = False,
    ) -> None:
        super().__init__()
        self.message = message
        self.title = title
        self.confirm_label = confirm_label
        self.danger = danger

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(self.title, classes="kicker")
            yield Static(self.message, id="confirm-message")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(
                    self.confirm_label,
                    id="confirm",
                    variant="error" if self.danger else "warning",
                )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)


class ProgressScreen(BaseScreen):
    """Run a blocking operation in a worker with a live log and cancel."""

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "noop", "Back", show=False)]

    def __init__(
        self,
        title: str,
        operation: Operation,
        *,
        on_success: Callable[[OpResult], None] | None = None,
        cancel_label: str = "Cancel",
    ) -> None:
        super().__init__()
        self.operation_title = title
        self.operation = operation
        self.on_success = on_success
        self.cancel_label = cancel_label
        self.cancel_event = threading.Event()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(id="progress-body"):
            yield Static(self.operation_title, classes="kicker")
            yield LoadingIndicator(id="spinner")
            yield RichLog(id="progress-log", markup=False, wrap=True, highlight=False)
            yield Static("", id="progress-result")
            with Horizontal(classes="buttons"):
                yield Button(self.cancel_label, id="cancel", variant="warning")
                yield Button("Close", id="close", disabled=True)
        yield Footer()

    def on_mount(self) -> None:
        self._log(f"starting: {self.operation_title}")
        self.run_worker(self._run, thread=True, exclusive=True)

    def _log(self, message: str) -> None:
        self.query_one("#progress-log", RichLog).write(message)

    def _run(self) -> None:
        def progress(message: str) -> None:
            self.app.call_from_thread(self._log, message)

        try:
            result = self.operation(progress, self.cancel_event)
        except Exception as exc:  # keep the UI alive and show the error
            result = OpResult(False, f"operation crashed: {exc}")
        self.app.call_from_thread(self._finish, result)

    def _finish(self, result: OpResult) -> None:
        self.query_one("#spinner", LoadingIndicator).display = False
        style = "success" if result.ok else "error"
        self.query_one("#progress-result", Static).update(
            f"[{style}]{result.message}[/{style}]"
        )
        self._log(result.message)
        self.query_one("#close", Button).disabled = False
        self.query_one("#cancel", Button).disabled = True
        if result.ok and self.on_success is not None:
            self.on_success(result)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.cancel_event.set()
            self._log("cancellation requested; finishing the current step")
            event.button.disabled = True
        elif event.button.id == "close":
            self.app.pop_screen()

    def action_noop(self) -> None:
        if self.query_one("#close", Button).disabled is False:
            self.app.pop_screen()


class SummaryScreen(BaseScreen):
    def __init__(self, result: OpResult) -> None:
        super().__init__()
        self.result = result

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(id="summary-body"):
            yield Static("Setup complete", classes="kicker")
            yield Static(self.result.message, id="summary-message", classes="success")
            yield Static("", id="summary-credentials")
            yield Static("Scan to join the hotspot", classes="kicker")
            yield Static("", id="summary-qr")
            with Horizontal(classes="buttons"):
                yield Button("Done", id="done", variant="success")
        yield Footer()

    def on_mount(self) -> None:
        payload: dict[str, Any] = (
            self.result.payload if isinstance(self.result.payload, dict) else {}
        )
        password = payload.get("admin_password")
        warnings = payload.get("warnings") or ()
        lines = [
            f"Dashboard: [bold]{self.hub.controller.dashboard_url()}[/bold]",
            "Wifi: [bold]EventHub[/bold]",
            "Users are managed from the dashboard.",
        ]
        if password:
            lines.append(f"Admin user: [bold]admin[/bold]  Password: [bold]{password}[/bold]")
            lines.append(
                "This password was also written to "
                f"{self.hub.controller.config.dashboard.first_boot_password_file}"
            )
        if warnings:
            lines.append("")
            lines.append("[yellow]Warnings:[/yellow]")
            lines.extend(f"  - {item}" for item in warnings)
        self.query_one("#summary-credentials", Static).update("\n".join(lines))
        try:
            self.query_one("#summary-qr", Static).update(self.hub.controller.wifi_qr())
        except Exception:
            self.query_one("#summary-qr", Static).update("QR code unavailable")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "done":
            self.hub.show_main_menu()
