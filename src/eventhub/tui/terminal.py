"""Terminal presentation backends.

The primary backend is whiptail/dialog, which gives Raspberry Pi OS the same
blue raspi-config style menus.  A plain numbered-menu fallback keeps the TUI
usable over a bare serial console or a system without newt installed.
"""

from __future__ import annotations

import contextlib
import getpass
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class MenuItem:
    tag: str
    label: str


@runtime_checkable
class TerminalBackend(Protocol):
    def menu(
        self,
        title: str,
        text: str,
        items: Sequence[MenuItem],
        *,
        default: str | None = None,
    ) -> str | None: ...

    def input(
        self, title: str, text: str, *, default: str = "", password: bool = False
    ) -> str | None: ...

    def yesno(self, title: str, text: str, *, default: bool = True) -> bool: ...

    def msgbox(self, title: str, text: str) -> None: ...

    def checklist(
        self, title: str, text: str, items: Sequence[tuple[str, str, bool]]
    ) -> list[str] | None: ...

    def textbox(self, title: str, text: str) -> None: ...


class WhiptailBackend:
    """whiptail/dialog menus with arrow keys, like raspi-config."""

    def __init__(self, program: str) -> None:
        self.program = program
        columns, rows = shutil.get_terminal_size((80, 24))
        self.width = max(50, min(columns - 4, 100))
        self.height = max(12, min(rows - 4, 30))

    def _run(self, args: Sequence[str], *, title: str) -> tuple[int, str, str]:
        argv = [
            self.program,
            "--title",
            title,
            "--backtitle",
            "EventHub · portable photo hub",
            *args,
        ]
        try:
            result = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                env=os.environ.copy(),
            )
        except OSError:
            return 127, "", ""
        return result.returncode, result.stdout, result.stderr

    def _box_size(self, list_height: int = 8) -> tuple[int, int, int]:
        height = min(self.height, max(12, list_height + 7))
        return height, self.width, min(list_height, height - 7)

    def menu(
        self,
        title: str,
        text: str,
        items: Sequence[MenuItem],
        *,
        default: str | None = None,
    ) -> str | None:
        height, width, list_height = self._box_size(len(items))
        args: list[str] = []
        if default:
            args.extend(["--default-item", default])
        args.extend(["--menu", text, str(height), str(width), str(list_height)])
        for item in items:
            args.extend([item.tag, item.label])
        rc, _out, err = self._run(args, title=title)
        if rc != 0:
            return None
        selected = shlex.split(err.strip())
        return selected[0] if selected else None

    def input(
        self, title: str, text: str, *, default: str = "", password: bool = False
    ) -> str | None:
        flag = "--passwordbox" if password else "--inputbox"
        height, width, _ = self._box_size(3)
        args = [flag, text, str(height), str(width), default]
        rc, _out, err = self._run(args, title=title)
        if rc != 0:
            return None
        return err.rstrip("\n")

    def yesno(self, title: str, text: str, *, default: bool = True) -> bool:
        height, width, _ = self._box_size(4)
        args = ([] if default else ["--defaultno"]) + [
            "--yesno",
            text,
            str(height),
            str(width),
        ]
        rc, _out, _err = self._run(args, title=title)
        return rc == 0

    def msgbox(self, title: str, text: str) -> None:
        height, width, _ = self._box_size(8)
        self._run(["--msgbox", text, str(height), str(width)], title=title)

    def checklist(
        self, title: str, text: str, items: Sequence[tuple[str, str, bool]]
    ) -> list[str] | None:
        height, width, list_height = self._box_size(len(items))
        args: list[str] = [
            "--checklist",
            text,
            str(height),
            str(width),
            str(list_height),
        ]
        for tag, label, checked in items:
            args.extend([tag, label, "on" if checked else "off"])
        rc, _out, err = self._run(args, title=title)
        if rc != 0:
            return None
        return shlex.split(err.strip())

    def textbox(self, title: str, text: str) -> None:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(text)
            name = handle.name
        try:
            height, width, _ = self._box_size(18)
            self._run(["--textbox", name, str(height), str(width)], title=title)
        finally:
            os.unlink(name)


class PlainBackend:
    """Numbered/typed fallback for consoles without whiptail."""

    def _header(self, title: str, text: str) -> None:
        print()
        print("=" * 72)
        print(title)
        print("-" * 72)
        if text:
            print(text)
            print()

    def _ask(self, prompt: str, default: str = "") -> str:
        try:
            value = input(prompt).strip()
        except EOFError:
            return ""
        return value or default

    def menu(
        self,
        title: str,
        text: str,
        items: Sequence[MenuItem],
        *,
        default: str | None = None,
    ) -> str | None:
        self._header(title, text)
        for index, item in enumerate(items, 1):
            marker = " (default)" if item.tag == default else ""
            print(f"  {index:2d}. {item.label}{marker}")
        print("   0. Back / Cancel")
        answer = self._ask("Select: ")
        if answer in {"", "0"}:
            return default if answer == "" else None
        try:
            return items[int(answer) - 1].tag
        except (ValueError, IndexError):
            return None

    def input(
        self, title: str, text: str, *, default: str = "", password: bool = False
    ) -> str | None:
        self._header(title, text)
        try:
            if password:
                value = getpass.getpass("Value (blank = keep/generate): ")
            else:
                value = input(f"Value [{default}]: ").strip()
        except EOFError:
            return None
        return value if value else default

    def yesno(self, title: str, text: str, *, default: bool = True) -> bool:
        self._header(title, text)
        suffix = "[Y/n]" if default else "[y/N]"
        answer = self._ask(f"{suffix} ").lower()
        if not answer:
            return default
        return answer in {"y", "yes"}

    def msgbox(self, title: str, text: str) -> None:
        self._header(title, text)
        with contextlib.suppress(EOFError):
            input("Press Enter to continue...")

    def checklist(
        self, title: str, text: str, items: Sequence[tuple[str, str, bool]]
    ) -> list[str] | None:
        self._header(title, text + "\nEnter numbers separated by commas; blank keeps the defaults.")
        selected = {tag for tag, _label, checked in items if checked}
        for index, (tag, label, checked) in enumerate(items, 1):
            mark = "x" if checked else " "
            print(f"  {index:2d}. [{mark}] {label} ({tag})")
        answer = self._ask("Numbers (0 = cancel): ")
        if answer == "0":
            return None
        if not answer:
            return list(selected)
        for part in answer.replace(" ", "").split(","):
            try:
                tag = items[int(part) - 1][0]
            except (ValueError, IndexError):
                continue
            if tag in selected:
                selected.discard(tag)
            else:
                selected.add(tag)
        return list(selected)

    def textbox(self, title: str, text: str) -> None:
        self._header(title, "")
        print(text)
        print()
        with contextlib.suppress(EOFError):
            input("Press Enter to continue...")


def make_backend(*, force_plain: bool = False) -> TerminalBackend:
    if force_plain or os.environ.get("EVENTHUB_PLAIN_TUI") == "1":
        return PlainBackend()
    if sys.stdin.isatty() and sys.stdout.isatty():
        for program in ("whiptail", "dialog"):
            binary = shutil.which(program)
            if binary:
                return WhiptailBackend(binary)
    return PlainBackend()


def clear_screen() -> None:
    if sys.stdout.isatty():
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()


def show_text(text: str) -> None:
    """Print terminal text (logs/QR) between full-screen dialogs."""

    print(text, flush=True)
