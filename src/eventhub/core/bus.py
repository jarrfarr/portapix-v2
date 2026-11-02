"""A tiny synchronous event bus.

Modules publish domain events (event activated, drive removed, uplink changed)
without importing each other.  Handlers are isolated: a failing subscriber is
logged and never breaks the publisher.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("eventhub.core.bus")


@dataclass(frozen=True, slots=True)
class Event:
    name: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    source: str = ""


Handler = Callable[[Event], None]


class EventBus:
    """Thread-safe pub/sub with exact-name and ``*`` wildcard subscribers."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._handlers: dict[str, list[Handler]] = {}
        self._history: list[Event] = []
        self._history_limit = 500

    def subscribe(self, name: str, handler: Handler) -> Callable[[], None]:
        with self._lock:
            self._handlers.setdefault(name, []).append(handler)

        def unsubscribe() -> None:
            with self._lock:
                handlers = self._handlers.get(name, [])
                if handler in handlers:
                    handlers.remove(handler)
                if not handlers:
                    self._handlers.pop(name, None)

        return unsubscribe

    def publish(self, event: Event) -> list[Exception]:
        with self._lock:
            handlers = list(self._handlers.get(event.name, ()))
            handlers.extend(self._handlers.get("*", ()))
            self._history.append(event)
            if len(self._history) > self._history_limit:
                self._history = self._history[-self._history_limit :]

        errors: list[Exception] = []
        for handler in handlers:
            try:
                handler(event)
            except Exception as exc:  # a subscriber must not break the system
                logger.exception(
                    "event handler %r failed for %s: %s", handler, event.name, exc
                )
                errors.append(exc)
        return errors

    def emit(
        self,
        name: str,
        payload: Mapping[str, Any] | None = None,
        *,
        source: str = "",
    ) -> list[Exception]:
        return self.publish(Event(name=name, payload=payload or {}, source=source))

    def history(self, name: str | None = None, limit: int = 100) -> list[Event]:
        with self._lock:
            items = self._history if name is None else [e for e in self._history if e.name == name]
            return list(items[-max(0, limit) :])
