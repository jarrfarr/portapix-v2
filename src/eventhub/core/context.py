"""The dependency-injection container handed to every service module."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from eventhub.core.adapters import (
    Clock,
    FileSystem,
    RealClock,
    RealFileSystem,
    RealRunner,
    Runner,
    Systemctl,
    Systemd,
    is_root,
)
from eventhub.core.bus import EventBus
from eventhub.core.config import AppConfig, load_config
from eventhub.core.db import Database, open_database
from eventhub.core.errors import PermissionDeniedError
from eventhub.core.logging import get_logger


@dataclass(slots=True)
class Runtime:
    """Everything a module is allowed to touch from the outside world."""

    config: AppConfig
    config_path: Path
    runner: Runner
    fs: FileSystem
    systemd: Systemd
    clock: Clock
    db: Database
    logger: logging.Logger
    bus: EventBus
    privileged: bool = False
    dry_run: bool = False
    extras: dict[str, Any] = field(default_factory=dict)

    def with_config(self, config: AppConfig) -> Runtime:
        return replace(self, config=config)

    def require_privilege(self, action: str) -> None:
        if not self.privileged:
            raise PermissionDeniedError(
                f"{action} requires root; run it through the EventHub helper or with sudo"
            )

    def log(self, name: str | None = None) -> logging.Logger:
        return get_logger(name) if name else self.logger

    def close(self) -> None:
        self.db.close()


def build_runtime(
    *,
    config: AppConfig | None = None,
    config_path: Path | str | None = None,
    runner: Runner | None = None,
    fs: FileSystem | None = None,
    systemd: Systemd | None = None,
    clock: Clock | None = None,
    db: Database | None = None,
    logger: logging.Logger | None = None,
    bus: EventBus | None = None,
    privileged: bool | None = None,
    dry_run: bool = False,
    extras: dict[str, Any] | None = None,
) -> Runtime:
    if config is None:
        path = Path("/etc/eventhub/config.yaml") if config_path is None else Path(config_path)
        config = load_config(path)
    else:
        path = Path(config_path) if config_path is not None else Path("/etc/eventhub/config.yaml")

    real_runner = runner or RealRunner()
    database = db or open_database(config.db_path)
    return Runtime(
        config=config,
        config_path=path,
        runner=real_runner,
        fs=fs or RealFileSystem(),
        systemd=systemd or Systemctl(real_runner),
        clock=clock or RealClock(),
        db=database,
        logger=logger or get_logger(),
        bus=bus or EventBus(),
        privileged=is_root() if privileged is None else privileged,
        dry_run=dry_run,
        extras=extras or {},
    )
