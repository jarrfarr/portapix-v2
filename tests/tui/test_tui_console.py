from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from eventhub.core.config import config_yaml
from eventhub.tui.app import ConsoleTUI, run_tui
from eventhub.tui.service import TuiController, qr_ascii, wifi_qr_payload
from eventhub.tui.setup import SetupState
from eventhub.tui.terminal import MenuItem, PlainBackend, WhiptailBackend
from tests.support import make_runtime


class ScriptedBackend:
    def __init__(
        self,
        *,
        menus: list[str | None] | None = None,
        inputs: list[str | None] | None = None,
        yesnos: list[bool] | None = None,
        checklists: list[list[str] | None] | None = None,
    ) -> None:
        self.menus = list(menus or [])
        self.inputs = list(inputs or [])
        self.yesnos = list(yesnos or [])
        self.checklists = list(checklists or [])
        self.messages: list[tuple[str, str]] = []
        self.texts: list[tuple[str, str]] = []
        self.menu_calls: list[str] = []

    def menu(
        self,
        title: str,
        text: str,
        items: list[MenuItem],
        *,
        default: str | None = None,
    ) -> str | None:
        self.menu_calls.append(title)
        return self.menus.pop(0) if self.menus else None

    def input(
        self, title: str, text: str, *, default: str = "", password: bool = False
    ) -> str | None:
        return self.inputs.pop(0) if self.inputs else default

    def yesno(self, title: str, text: str, *, default: bool = True) -> bool:
        return self.yesnos.pop(0) if self.yesnos else default

    def msgbox(self, title: str, text: str) -> None:
        self.messages.append((title, text))

    def checklist(
        self, title: str, text: str, items: list[tuple[str, str, bool]]
    ) -> list[str] | None:
        if self.checklists:
            return self.checklists.pop(0)
        return [tag for tag, _label, checked in items if checked]

    def textbox(self, title: str, text: str) -> None:
        self.texts.append((title, text))


def installed_runtime(tmp_path: Path):
    runtime = make_runtime(tmp_path)
    runtime.fs.atomic_write(runtime.config_path, config_yaml(runtime.config))
    return runtime


def test_main_menu_quit(tmp_path: Path) -> None:
    runtime = installed_runtime(tmp_path)
    backend = ScriptedBackend(menus=["quit"])
    ui = ConsoleTUI(TuiController(runtime.config_path, runtime=runtime), backend)
    assert ui.run() == 0
    assert backend.menu_calls == ["EventHub"]


def test_first_run_cancel_returns_one(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)  # no config on disk
    backend = ScriptedBackend(menus=[None])
    ui = ConsoleTUI(TuiController(runtime.config_path, runtime=runtime), backend)
    assert ui.run() == 1
    assert any("Welcome" in title for title, _text in backend.messages)


def test_wizard_state_build_patch(tmp_path: Path) -> None:
    runtime = installed_runtime(tmp_path)
    controller = TuiController(runtime.config_path, runtime=runtime)
    state = SetupState()
    state.initialise(controller)
    state.roles = {
        "ap": "wlan9",
        "airdrop": "wlx9",
        "wan": "eth9",
        "lan": "eth8",
    }
    state.modules = {info.name: True for info in controller.infos() if info.available}
    state.modules["airdrop"] = False
    state.drive_uuid = "ABCD-1234"
    patch = state.build_patch(controller)
    assert patch["roles"]["ap"] == "wlan9"
    assert patch["hotspot"]["interface"] == "wlan9"
    assert patch["airdrop"]["enabled"] is False
    assert patch["storage"]["drive"]["uuid"] == "ABCD-1234"
    assert patch["fileserver_smb"]["enabled"] is True


def test_modules_details_flow(tmp_path: Path) -> None:
    runtime = installed_runtime(tmp_path)
    backend = ScriptedBackend(
        menus=["modules", "hotspot", "details", None, "quit"]
    )
    ui = ConsoleTUI(TuiController(runtime.config_path, runtime=runtime), backend)
    assert ui.run() == 0
    assert backend.texts
    assert "hotspot" in backend.texts[0][0]


def test_run_tui_entry_point(tmp_path: Path) -> None:
    runtime = installed_runtime(tmp_path)
    backend = ScriptedBackend(menus=["quit"])
    code = run_tui(
        config=runtime.config,
        config_path=runtime.config_path,
        runtime=runtime,
        backend=backend,
    )
    assert code == 0


def test_plain_backend_menu(monkeypatch) -> None:
    backend = PlainBackend()
    monkeypatch.setattr("builtins.input", lambda prompt="": "2")
    result = backend.menu(
        "Test",
        "Choose",
        [MenuItem("one", "One"), MenuItem("two", "Two")],
    )
    assert result == "two"


def test_whiptail_arguments_put_options_before_box(monkeypatch) -> None:
    captured: list[list[str]] = []

    def fake_run(argv, **kwargs):
        captured.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="status\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    backend = WhiptailBackend("/usr/bin/whiptail")
    result = backend.menu(
        "EventHub",
        "Pick",
        [MenuItem("status", "Status"), MenuItem("quit", "Quit")],
        default="status",
    )
    assert result == "status"
    argv = captured[0]
    assert argv.index("--default-item") < argv.index("--menu")
    assert "status" in argv and "quit" in argv


def test_qr_code_renders() -> None:
    payload = wifi_qr_payload("TestNet", "password123")
    text = qr_ascii(payload)
    assert "\n" in text
    assert "█" in text or "▀" in text or "▄" in text
