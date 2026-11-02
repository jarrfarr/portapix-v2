"""Logging setup with rotation, console output and an install log."""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
import os
import sys
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
INSTALL_LOG = Path("/var/log/eventhub-install.log")
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5


class _EnsureDirFileHandler(logging.handlers.RotatingFileHandler):
    def __init__(self, filename: str | Path, **kwargs: object) -> None:
        path = Path(filename)
        with contextlib.suppress(OSError):
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        super().__init__(str(path), **kwargs)  # type: ignore[arg-type]


def get_logger(name: str | None = None) -> logging.Logger:
    base = "eventhub"
    if name is None or name == base:
        return logging.getLogger(base)
    if name.startswith(f"{base}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{base}.{name}")


def configure_logging(
    *,
    level: str | int = "INFO",
    log_dir: Path | str | None = None,
    filename: str = "eventhub.log",
    console: bool = True,
    install_log: Path | str | None = None,
    force: bool = False,
) -> logging.Logger:
    """Idempotently configure the ``eventhub`` logger tree."""

    logger = logging.getLogger("eventhub")
    logger.setLevel(level)
    logger.propagate = False

    if force:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            with contextlib.suppress(Exception):
                handler.close()
    elif logger.handlers:
        return logger

    formatter = logging.Formatter(LOG_FORMAT)

    if log_dir is not None:
        try:
            log_path = Path(log_dir) / filename
            file_handler = _EnsureDirFileHandler(
                log_path,
                maxBytes=MAX_BYTES,
                backupCount=BACKUP_COUNT,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            file_handler.setLevel(level)
            logger.addHandler(file_handler)
        except OSError as exc:
            logger.warning("could not open log file in %s: %s", log_dir, exc)

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        stream.setLevel(level)
        logger.addHandler(stream)

    if install_log is not None:
        try:
            inst = _EnsureDirFileHandler(
                install_log,
                maxBytes=MAX_BYTES,
                backupCount=BACKUP_COUNT,
                encoding="utf-8",
            )
            inst.setFormatter(formatter)
            inst.setLevel(level)
            logger.addHandler(inst)
        except OSError:
            pass

    return logger


def log_marker(logger: logging.Logger, level: str, message: str) -> None:
    """Write an install-log style ``[ok]``/``[warn]``/``[fail]`` line."""

    marker = {"ok": "[ok]", "warn": "[warn]", "fail": "[fail]", "info": "[info]"}.get(
        level, "[info]"
    )
    log_level = {
        "ok": logging.INFO,
        "info": logging.INFO,
        "warn": logging.WARNING,
        "fail": logging.ERROR,
    }.get(level, logging.INFO)
    logger.log(log_level, "%s %s", marker, message)


def tail_file(path: Path | str, *, lines: int = 200, max_bytes: int = 512_000) -> list[str]:
    """Read the tail of a file without loading an unbounded log into memory."""

    target = Path(path)
    try:
        size = target.stat().st_size
        with target.open("rb") as handle:
            if size > max_bytes:
                handle.seek(-max_bytes, os.SEEK_END)
            data = handle.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    return text.splitlines()[-max(0, lines) :]
