"""Reusable fakes for unit tests.  No Raspberry Pi, root or systemd needed."""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from eventhub.core.adapters import StatVfs
from eventhub.core.bus import EventBus
from eventhub.core.config import (
    AirdropConfig,
    AppConfig,
    CopypartyConfig,
    DashboardConfig,
    DiscoveryConfig,
    EventsConfig,
    HelperConfig,
    HotspotConfig,
    InstallConfig,
    NetworkConfig,
    SmbConfig,
    StorageConfig,
    SystemConfig,
    UsersConfig,
)
from eventhub.core.context import Runtime
from eventhub.core.db import open_database
from eventhub.core.logging import get_logger
from eventhub.core.types import CommandResult


@dataclass
class FakeRunner:
    responses: dict[str, CommandResult] = field(default_factory=dict)
    calls: list[tuple[str, ...]] = field(default_factory=list)
    binaries: set[str] = field(default_factory=lambda: {"systemctl", "apt-get", "git"})
    default: CommandResult | None = None

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = False,
        timeout: float = 30.0,
        input_text: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: Path | str | None = None,
    ) -> CommandResult:
        args = tuple(str(a) for a in argv)
        self.calls.append(args)
        result = self._match(args)
        if check and not result.ok:
            from eventhub.core.errors import CommandError

            raise CommandError(args, result.returncode, result.stderr)
        return result

    def _match(self, args: tuple[str, ...]) -> CommandResult:
        joined = " ".join(args)
        for pattern, response in self.responses.items():
            if fnmatch.fnmatchcase(joined, pattern) or joined.startswith(pattern):
                return response
        return self.default or CommandResult(argv=args, returncode=0)

    def which(self, program: str) -> str | None:
        return f"/usr/bin/{program}" if program in self.binaries else None

    def called(self, pattern: str) -> bool:
        regex = re.compile(pattern)
        return any(regex.search(" ".join(call)) for call in self.calls)

    def command_count(self, pattern: str) -> int:
        regex = re.compile(pattern)
        return sum(1 for call in self.calls if regex.search(" ".join(call)))


class FakeFileSystem:
    """In-memory filesystem implementing the EventHub FileSystem protocol."""

    def __init__(self, *, free_bytes: int = 500 * 1024**3, total_bytes: int = 1024**3) -> None:
        self.files: dict[Path, str] = {}
        self.modes: dict[Path, int] = {}
        self.directories: set[Path] = {Path("/")}
        self.links: dict[Path, Path] = {}
        self.owners: dict[Path, tuple[str, str | None]] = {}
        self.free_bytes = free_bytes
        self.total_bytes = total_bytes

    @staticmethod
    def _p(path: Path | str) -> Path:
        value = Path(path)
        if not value.is_absolute():
            value = Path("/") / value
        parts: list[str] = []
        for part in value.parts:
            if part in {"", "/", "."}:
                continue
            if part == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(part)
        return Path("/").joinpath(*parts)

    def _ensure_parents(self, path: Path) -> None:
        self.directories.add(Path("/"))
        for parent in path.parents:
            if parent != Path("."):
                self.directories.add(parent)

    def exists(self, path: Path | str) -> bool:
        value = self._p(path)
        return value in self.files or value in self.directories or value in self.links

    def is_file(self, path: Path | str) -> bool:
        return self._p(path) in self.files

    def is_dir(self, path: Path | str) -> bool:
        return self._p(path) in self.directories

    def is_symlink(self, path: Path | str) -> bool:
        return self._p(path) in self.links

    def read_text(self, path: Path | str) -> str:
        value = self._p(path)
        if value not in self.files:
            raise FileNotFoundError(value)
        return self.files[value]

    def write_text(self, path: Path | str, content: str, *, mode: int = 0o644) -> None:
        value = self._p(path)
        self._ensure_parents(value)
        self.files[value] = content
        self.modes[value] = mode

    def atomic_write(self, path: Path | str, content: str, *, mode: int = 0o644) -> None:
        self.write_text(path, content, mode=mode)

    def mkdir(self, path: Path | str, *, mode: int = 0o750, parents: bool = True) -> None:
        value = self._p(path)
        if parents:
            self._ensure_parents(value)
        self.directories.add(value)
        self.modes.setdefault(value, mode)

    def remove(self, path: Path | str, *, missing_ok: bool = True) -> None:
        value = self._p(path)
        if value in self.files:
            del self.files[value]
            self.modes.pop(value, None)
        elif value in self.links:
            del self.links[value]
        elif value in self.directories:
            self.directories.remove(value)
        elif not missing_ok:
            raise FileNotFoundError(value)

    def rmtree(self, path: Path | str, *, missing_ok: bool = True) -> None:
        value = self._p(path)
        found = any(p == value or value in p.parents for p in self.files)
        found = found or any(p == value or value in p.parents for p in self.directories)
        if not found and not missing_ok:
            raise FileNotFoundError(value)
        for p in list(self.files):
            if p == value or value in p.parents:
                del self.files[p]
                self.modes.pop(p, None)
        for p in list(self.directories):
            if p == value or value in p.parents:
                self.directories.discard(p)
        for p in list(self.links):
            if p == value or value in p.parents:
                del self.links[p]

    def listdir(self, path: Path | str) -> list[str]:
        value = self._p(path)
        if value not in self.directories:
            raise FileNotFoundError(value)
        names = {
            p.name
            for p in (*self.files, *self.directories, *self.links)
            if p != value and p.parent == value
        }
        return sorted(names)

    def chmod(self, path: Path | str, mode: int) -> None:
        self.modes[self._p(path)] = mode

    def mode_of(self, path: Path | str) -> int:
        value = self._p(path)
        if value not in self.files and value not in self.directories:
            raise FileNotFoundError(value)
        return self.modes.get(value, 0o644)

    def chown(self, path: Path | str, owner: str, group: str | None = None) -> None:
        self.owners[self._p(path)] = (owner, group)

    def symlink(self, target: Path | str, link: Path | str) -> None:
        value = self._p(link)
        self._ensure_parents(value)
        self.links[value] = Path(target)

    def readlink(self, path: Path | str) -> Path:
        return self.links[self._p(path)]

    def statvfs(self, path: Path | str) -> StatVfs:
        used = self.total_bytes - self.free_bytes
        return StatVfs(
            total_bytes=self.total_bytes,
            free_bytes=self.free_bytes,
            available_bytes=self.free_bytes,
            used_bytes=used,
        )

    def rename(self, source: Path | str, target: Path | str) -> None:
        src, dst = self._p(source), self._p(target)
        if src in self.files:
            self.files[dst] = self.files.pop(src)
            self.modes[dst] = self.modes.pop(src, 0o644)
        elif src in self.links:
            self.links[dst] = self.links.pop(src)

    # convenience assertions
    def snapshot(self) -> dict[str, str]:
        return {str(path): content for path, content in self.files.items()}

    def find(self, suffix: str) -> Path | None:
        for path in self.files:
            if str(path).endswith(suffix):
                return path
        return None


@dataclass
class FakeSystemd:
    active: set[str] = field(default_factory=set)
    enabled: set[str] = field(default_factory=set)
    existing: set[str] = field(default_factory=set)
    actions: list[tuple[str, str]] = field(default_factory=list)
    daemon_reloads: int = 0

    @staticmethod
    def _unit(unit: str) -> str:
        return unit if "." in Path(unit).name else f"{unit}.service"

    def is_active(self, unit: str) -> bool:
        return self._unit(unit) in self.active

    def is_enabled(self, unit: str) -> bool:
        return self._unit(unit) in self.enabled

    def _do(self, action: str, unit: str) -> bool:
        name = self._unit(unit)
        self.actions.append((action, name))
        if action == "start":
            self.active.add(name)
        elif action == "stop":
            self.active.discard(name)
        elif action == "restart":
            self.active.add(name)
        elif action == "enable":
            self.enabled.add(name)
        elif action == "disable":
            self.enabled.discard(name)
        return True

    def start(self, unit: str) -> bool:
        return self._do("start", unit)

    def stop(self, unit: str) -> bool:
        return self._do("stop", unit)

    def restart(self, unit: str) -> bool:
        return self._do("restart", unit)

    def reload(self, unit: str) -> bool:
        return self._do("reload", unit)

    def enable(self, unit: str) -> bool:
        return self._do("enable", unit)

    def disable(self, unit: str) -> bool:
        return self._do("disable", unit)

    def daemon_reload(self) -> bool:
        self.daemon_reloads += 1
        return True

    def status_text(self, unit: str) -> str:
        return f"{unit}: active" if self.is_active(unit) else f"{unit}: inactive"

    def journal(self, unit: str, *, lines: int = 200) -> str:
        return "\n".join(f"{unit} log line {i}" for i in range(min(lines, 5)))


@dataclass
class FakeClock:
    current: datetime = field(default_factory=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    tick: float = 0.0

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return self.tick

    def sleep(self, seconds: float) -> None:
        self.tick += seconds
        self.current += timedelta(seconds=seconds)


def make_config(
    tmp_path: Path,
    *,
    enabled: bool = True,
    **overrides: Any,
) -> AppConfig:
    """Create a fully valid config rooted inside a temporary directory."""

    data_root = tmp_path / "data"
    state_dir = tmp_path / "state"
    log_dir = tmp_path / "log"
    values: dict[str, Any] = {
        "data_root": data_root,
        "state_dir": state_dir,
        "log_dir": log_dir,
        "roles": {"ap": "wlan-test", "airdrop": "wlx-test", "wan": "eth-wan", "lan": "eth-lan"},
        "hotspot": HotspotConfig(
            enabled=enabled,
            ssid="TestHub",
            password="test-password-123",  # type: ignore[arg-type]
            subnet="10.42.0.0/24",
            dhcp_start="10.42.0.10",
            dhcp_end="10.42.0.200",
        ),
        "network": NetworkConfig(enabled=enabled),
        "storage": StorageConfig(
            enabled=enabled,
            mount_root=tmp_path / "media",
            drive={"mount_point": tmp_path / "media" / "data"},
        ),
        "users": UsersConfig(enabled=enabled),
        "events": EventsConfig(enabled=enabled, root=data_root / "events"),
        "fileserver_copyparty": CopypartyConfig(
            enabled=enabled,
            config_path=tmp_path / "etc" / "copyparty.conf",
            home_dir=tmp_path / "state" / "copyparty",
        ),
        "fileserver_smb": SmbConfig(
            enabled=enabled,
            config_path=tmp_path / "etc" / "smb.conf",
        ),
        "airdrop": AirdropConfig(
            enabled=enabled,
            target_dir=data_root / "inbox",
            owl_dir=tmp_path / "opt" / "owl",
        ),
        "discovery": DiscoveryConfig(enabled=enabled, hostname="testhub"),
        "dashboard": DashboardConfig(
            enabled=enabled,
            first_boot_password_file=state_dir / "first-boot-admin.txt",
            log_dir=log_dir,
        ),
        "helper": HelperConfig(socket_path=tmp_path / "run" / "helper.sock"),
        "system": SystemConfig(enabled=enabled),
        "install": InstallConfig(
            install_root=tmp_path / "opt",
            venv_path=tmp_path / "opt" / "venv",
            config_path=tmp_path / "etc" / "config.yaml",
            state_dir=state_dir,
            data_root=data_root,
            log_file=tmp_path / "log" / "install.log",
            admin_password_file=state_dir / "first-boot-admin.txt",
        ),
    }
    values.update(overrides)
    return AppConfig.model_validate(values)


def make_runtime(
    tmp_path: Path,
    *,
    config: AppConfig | None = None,
    fs: FakeFileSystem | None = None,
    runner: FakeRunner | None = None,
    systemd: FakeSystemd | None = None,
    clock: FakeClock | None = None,
    privileged: bool = True,
    dry_run: bool = False,
) -> Runtime:
    cfg = config or make_config(tmp_path)
    db = open_database(tmp_path / "state" / "eventhub.db")
    runtime = Runtime(
        config=cfg,
        config_path=tmp_path / "etc" / "config.yaml",
        runner=runner or FakeRunner(),
        fs=fs or FakeFileSystem(),
        systemd=systemd or FakeSystemd(),
        clock=clock or FakeClock(),
        db=db,
        logger=get_logger("tests"),
        bus=EventBus(),
        privileged=privileged,
        dry_run=dry_run,
    )
    return runtime
