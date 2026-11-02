from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import DataTable, ListView

from eventhub.core.config import config_yaml
from eventhub.tui.app import EventHubApp
from eventhub.tui.screens import (
    BackupScreen,
    ConfigureScreen,
    MainMenuScreen,
    ModulesScreen,
    RecoveryScreen,
    StatusScreen,
    UninstallScreen,
)
from eventhub.tui.service import qr_ascii, wifi_qr_payload
from eventhub.tui.wizard import WizardHardwareScreen, WizardInterfacesScreen
from tests.support import make_runtime


def installed_app(tmp_path: Path) -> EventHubApp:
    runtime = make_runtime(tmp_path)
    runtime.fs.atomic_write(runtime.config_path, config_yaml(runtime.config))
    return EventHubApp(config_path=runtime.config_path, runtime=runtime)


def fresh_app(tmp_path: Path) -> EventHubApp:
    runtime = make_runtime(tmp_path)
    return EventHubApp(config_path=runtime.config_path, runtime=runtime)


@pytest.mark.asyncio
async def test_first_boot_opens_wizard(tmp_path: Path) -> None:
    app = fresh_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, WizardHardwareScreen)
        assert "Model:" in str(app.screen.query_one("#hardware-report").render())


@pytest.mark.asyncio
async def test_installed_opens_main_menu(tmp_path: Path) -> None:
    app = installed_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, MainMenuScreen)
        menu = app.screen.query_one("#main-menu", ListView)
        assert len(menu.children) == 9


@pytest.mark.asyncio
async def test_wizard_next_goes_to_interfaces(tmp_path: Path) -> None:
    app = fresh_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.query_one("#next").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, WizardInterfacesScreen)


@pytest.mark.asyncio
async def test_modules_screen_shows_table(tmp_path: Path) -> None:
    app = installed_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await app.push_screen(ModulesScreen())
        await pilot.pause()
        table = app.screen.query_one("#module-table", DataTable)
        assert table.row_count > 0


@pytest.mark.asyncio
async def test_status_and_configure_screens_render(tmp_path: Path) -> None:
    app = installed_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await app.push_screen(StatusScreen())
        await pilot.pause()
        assert app.screen.query_one("#status-table", DataTable).row_count > 0
        await app.pop_screen()
        await app.push_screen(ConfigureScreen())
        await pilot.pause()
        assert app.screen.query_one("#cfg-ssid")


@pytest.mark.asyncio
async def test_backup_recovery_uninstall_render(tmp_path: Path) -> None:
    app = installed_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        for screen in (BackupScreen(), RecoveryScreen(), UninstallScreen()):
            await app.push_screen(screen)
            await pilot.pause()
            assert app.screen is not None
            await app.pop_screen()
            await pilot.pause()


def test_qr_code_renders() -> None:
    text = qr_ascii(wifi_qr_payload("TestNet", "password123"))
    assert "█" in text or "▀" in text or "▄" in text
    assert "\n" in text


def test_wifi_qr_escapes_special_characters() -> None:
    payload = wifi_qr_payload('My;Net,1', "p:a\\ss")
    assert "\\;" in payload
    assert "\\," in payload
    assert "\\:" in payload
    assert "\\\\" in payload
