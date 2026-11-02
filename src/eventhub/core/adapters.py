"""OS adapters.

Every module receives these through :class:`eventhub.core.context.Runtime`.
Tests substitute small fakes, so the whole package runs on a laptop without a
Raspberry Pi, root or systemd.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from eventhub.core.errors import CommandError
from eventhub.core.logging import get_logger
from eventhub.core.types import CommandResult

logger = get_logger("core.adapters")


@runtime_checkable
class Runner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = False,
        timeout: float = 30.0,
        input_text: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: Path | str | None = None,
    ) -> CommandResult: ...

    def which(self, program: str) -> str | None: ...


class RealRunner:
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
        args = tuple(str(arg) for arg in argv)
        if not args:
            raise ValueError("argv must not be empty")
        logger.debug("run %s", " ".join(args))
        full_env = None
        if env is not None:
            full_env = {**os.environ, **env}
        try:
            proc = subprocess.run(
                args,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                input=input_text,
                env=full_env,
                cwd=str(cwd) if cwd is not None else None,
            )
        except FileNotFoundError as exc:
            result = CommandResult(argv=args, returncode=127, stderr=str(exc))
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            result = CommandResult(
                argv=args,
                returncode=124,
                stdout=stdout,
                stderr=stderr or f"timed out after {timeout}s",
            )
        else:
            result = CommandResult(
                argv=args,
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
            )
        if check and not result.ok:
            raise CommandError(args, result.returncode, result.stderr)
        if not result.ok:
            logger.debug("command failed (%s): %s", result.returncode, result.stderr.strip())
        return result

    def which(self, program: str) -> str | None:
        return shutil.which(program)


@dataclass(frozen=True, slots=True)
class StatVfs:
    total_bytes: int
    free_bytes: int
    available_bytes: int
    used_bytes: int

    @property
    def used_percent(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return round(self.used_bytes * 100.0 / self.total_bytes, 1)


@runtime_checkable
class FileSystem(Protocol):
    def exists(self, path: Path | str) -> bool: ...
    def is_file(self, path: Path | str) -> bool: ...
    def is_dir(self, path: Path | str) -> bool: ...
    def is_symlink(self, path: Path | str) -> bool: ...
    def read_text(self, path: Path | str) -> str: ...
    def write_text(self, path: Path | str, content: str, *, mode: int = 0o644) -> None: ...
    def atomic_write(
        self, path: Path | str, content: str, *, mode: int = 0o644
    ) -> None: ...
    def mkdir(
        self, path: Path | str, *, mode: int = 0o750, parents: bool = True
    ) -> None: ...
    def remove(self, path: Path | str, *, missing_ok: bool = True) -> None: ...
    def rmtree(self, path: Path | str, *, missing_ok: bool = True) -> None: ...
    def listdir(self, path: Path | str) -> list[str]: ...
    def chmod(self, path: Path | str, mode: int) -> None: ...
    def mode_of(self, path: Path | str) -> int: ...
    def chown(self, path: Path | str, owner: str, group: str | None = None) -> None: ...
    def symlink(self, target: Path | str, link: Path | str) -> None: ...
    def readlink(self, path: Path | str) -> Path: ...
    def statvfs(self, path: Path | str) -> StatVfs: ...
    def size_bytes(self, path: Path | str) -> int: ...
    def rename(self, source: Path | str, target: Path | str) -> None: ...


class RealFileSystem:
    def exists(self, path: Path | str) -> bool:
        return Path(path).exists()

    def is_file(self, path: Path | str) -> bool:
        return Path(path).is_file()

    def is_dir(self, path: Path | str) -> bool:
        return Path(path).is_dir()

    def is_symlink(self, path: Path | str) -> bool:
        return Path(path).is_symlink()

    def read_text(self, path: Path | str) -> str:
        return Path(path).read_text(encoding="utf-8")

    def write_text(self, path: Path | str, content: str, *, mode: int = 0o644) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        target.write_text(content, encoding="utf-8")
        os.chmod(target, mode)

    def atomic_write(self, path: Path | str, content: str, *, mode: int = 0o644) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_name, mode)
            os.replace(tmp_name, target)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name)
            raise

    def mkdir(self, path: Path | str, *, mode: int = 0o750, parents: bool = True) -> None:
        Path(path).mkdir(mode=mode, parents=parents, exist_ok=True)

    def remove(self, path: Path | str, *, missing_ok: bool = True) -> None:
        Path(path).unlink(missing_ok=missing_ok)

    def rmtree(self, path: Path | str, *, missing_ok: bool = True) -> None:
        target = Path(path)
        if missing_ok and not target.exists():
            return
        shutil.rmtree(target)

    def listdir(self, path: Path | str) -> list[str]:
        return sorted(os.listdir(path))

    def chmod(self, path: Path | str, mode: int) -> None:
        os.chmod(path, mode)

    def mode_of(self, path: Path | str) -> int:
        return Path(path).stat().st_mode & 0o777

    def chown(self, path: Path | str, owner: str, group: str | None = None) -> None:
        if os.geteuid() != 0:
            return
        import grp
        import pwd

        uid = pwd.getpwnam(owner).pw_uid
        gid = grp.getgrnam(group).gr_gid if group else -1
        os.chown(path, uid, gid)

    def symlink(self, target: Path | str, link: Path | str) -> None:
        Path(link).parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, link)

    def readlink(self, path: Path | str) -> Path:
        return Path(os.readlink(path))

    def statvfs(self, path: Path | str) -> StatVfs:
        stat = os.statvfs(path)
        total = stat.f_frsize * stat.f_blocks
        free = stat.f_frsize * stat.f_bfree
        available = stat.f_frsize * stat.f_bavail
        used = total - free
        return StatVfs(
            total_bytes=total,
            free_bytes=free,
            available_bytes=available,
            used_bytes=used,
        )

    def size_bytes(self, path: Path | str) -> int:
        target = Path(path)
        if target.is_file():
            return target.stat().st_size
        total = 0
        for root, _dirs, files in os.walk(target):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    continue
        return total

    def rename(self, source: Path | str, target: Path | str) -> None:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)


@runtime_checkable
class Systemd(Protocol):
    def is_active(self, unit: str) -> bool: ...
    def is_enabled(self, unit: str) -> bool: ...
    def start(self, unit: str) -> bool: ...
    def stop(self, unit: str) -> bool: ...
    def restart(self, unit: str) -> bool: ...
    def reload(self, unit: str) -> bool: ...
    def enable(self, unit: str) -> bool: ...
    def disable(self, unit: str) -> bool: ...
    def daemon_reload(self) -> bool: ...
    def status_text(self, unit: str) -> str: ...
    def journal(self, unit: str, *, lines: int = 200) -> str: ...


class Systemctl:
    def __init__(self, runner: Runner | None = None) -> None:
        self.runner = runner or RealRunner()

    @staticmethod
    def _unit(unit: str) -> str:
        if "." in Path(unit).name:
            return unit
        return f"{unit}.service"

    def _action(self, action: str, unit: str) -> bool:
        result = self.runner.run(
            ["systemctl", action, "--no-pager", "--quiet", self._unit(unit)],
            timeout=60,
        )
        return result.ok

    def is_active(self, unit: str) -> bool:
        return self.runner.run(
            ["systemctl", "is-active", "--quiet", self._unit(unit)], timeout=10
        ).ok

    def is_enabled(self, unit: str) -> bool:
        return self.runner.run(
            ["systemctl", "is-enabled", "--quiet", self._unit(unit)], timeout=10
        ).ok

    def start(self, unit: str) -> bool:
        return self._action("start", unit)

    def stop(self, unit: str) -> bool:
        return self._action("stop", unit)

    def restart(self, unit: str) -> bool:
        return self._action("restart", unit)

    def reload(self, unit: str) -> bool:
        return self._action("reload", unit)

    def enable(self, unit: str) -> bool:
        return self._action("enable", unit)

    def disable(self, unit: str) -> bool:
        return self._action("disable", unit)

    def daemon_reload(self) -> bool:
        return self.runner.run(["systemctl", "daemon-reload"], timeout=30).ok

    def status_text(self, unit: str) -> str:
        result = self.runner.run(
            ["systemctl", "status", "--no-pager", "--full", self._unit(unit)],
            timeout=15,
        )
        return (result.stdout + result.stderr).strip()

    def journal(self, unit: str, *, lines: int = 200) -> str:
        result = self.runner.run(
            [
                "journalctl",
                "--no-pager",
                "-n",
                str(max(1, lines)),
                "-u",
                self._unit(unit),
            ],
            timeout=20,
        )
        return result.stdout.strip()


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class RealClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def is_root() -> bool:
    return os.geteuid() == 0
