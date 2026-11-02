"""Core building blocks shared by every EventHub module.

The core package owns configuration, persistence, logging, the event bus and
the small OS adapter interfaces.  It deliberately knows nothing about any
individual service module.
"""

from __future__ import annotations

from eventhub.core.bus import Event, EventBus
from eventhub.core.config import (
    AppConfig,
    default_config,
    dump_config,
    load_config,
    save_config,
)
from eventhub.core.db import Database
from eventhub.core.errors import ConfigError
from eventhub.core.logging import configure_logging, get_logger

__all__ = [
    "AppConfig",
    "ConfigError",
    "Database",
    "Event",
    "EventBus",
    "configure_logging",
    "default_config",
    "dump_config",
    "get_logger",
    "load_config",
    "save_config",
]
