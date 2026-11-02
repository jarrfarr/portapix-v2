"""Textual application entry point for the EventHub terminal UI."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import ClassVar

from textual.app import App
from textual.binding import Binding

from eventhub.core.config import DEFAULT_CONFIG_PATH, AppConfig, load_config
from eventhub.core.errors import ConfigError
from eventhub.core.logging import configure_logging
from eventhub.core.types import ActionResult
from eventhub.installer.manager import Installer
from eventhub.tui.screens import MainMenuScreen
from eventhub.tui.service import TuiController
from eventhub.tui.wizard import WizardHardwareScreen, WizardState

CSS = """
Screen {
    background: $surface;
}
#menu-body, #screen-body, #wizard-body, #progress-body, #summary-body {
    padding: 1 2;
}
#menu-title {
    text-style: bold;
    color: $accent;
}
.kicker {
    text-style: bold;
    color: $accent;
    margin-bottom: 1;
}
.muted {
    color: $text-muted;
}
.field-label {
    margin-top: 1;
    text-style: bold;
}
.buttons {
    height: auto;
    margin-top: 1;
}
.buttons Button {
    margin-right: 1;
}
#main-menu {
    height: auto;
    max-height: 16;
    border: round $primary;
}
#main-menu ListItem {
    padding: 0 1;
}
#menu-status {
    margin-top: 1;
}
DataTable {
    height: 12;
}
#status-log, #progress-log {
    height: 10;
    border: round $primary;
    margin-top: 1;
}
.error {
    color: $error;
}
.success {
    color: $success;
}
ConfirmScreen {
    align: center middle;
    background: $background 60%;
}
#confirm-box {
    width: 72;
    height: auto;
    padding: 1 2;
    border: thick $warning;
    background: $panel;
}
#confirm-box #confirm-message {
    margin: 1 0;
}
#summary-qr {
    margin: 1 0;
}
"""


class EventHubApp(App[None]):
    TITLE = "EventHub"
    SUB_TITLE = "Portable photo hub"
    CSS = CSS
    BINDINGS: ClassVar[list[Binding]] = [Binding("ctrl+q", "quit_app", "Quit")]

    def __init__(
        self,
        *,
        config_path: Path | str = DEFAULT_CONFIG_PATH,
        config: AppConfig | None = None,
        dry_run: bool = False,
        runtime: object | None = None,
    ) -> None:
        super().__init__()
        self.controller = TuiController(
            config_path,
            dry_run=dry_run,
            runtime=runtime,  # type: ignore[arg-type]
        )
        if config is not None:
            self.controller.runtime.config = config
        self.wizard = WizardState()

    def on_mount(self) -> None:
        if self.controller.installed:
            self.push_screen(MainMenuScreen())
        else:
            self.push_screen(WizardHardwareScreen())

    def action_quit_app(self) -> None:
        self.exit()

    def show_main_menu(self) -> None:
        while len(self.screen_stack) > 1:
            self.pop_screen()
        self.switch_screen(MainMenuScreen())

    def on_unmount(self) -> None:
        with contextlib.suppress(Exception):
            self.controller.runtime.close()

    def reinstall_module(self, name: str) -> ActionResult:
        return Installer(self.controller.runtime).reinstall_module(name)


def run_tui(
    *,
    config: AppConfig | None,
    config_path: Path | str = DEFAULT_CONFIG_PATH,
    dry_run: bool = False,
    runtime: object | None = None,
) -> int:
    path = Path(config_path)
    log_dir = None
    if config is not None:
        log_dir = config.log_dir
    elif path.exists():
        try:
            log_dir = load_config(path).log_dir
        except ConfigError:
            log_dir = None
    configure_logging(level="INFO", log_dir=log_dir, console=False)
    app = EventHubApp(
        config_path=path,
        config=config,
        dry_run=dry_run,
        runtime=runtime,
    )
    app.run()
    return 0


def main() -> None:
    raise SystemExit(run_tui(config=None))
