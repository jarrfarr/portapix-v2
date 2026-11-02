"""Exception hierarchy for EventHub."""

from __future__ import annotations


class EventHubError(Exception):
    """Base class for all EventHub errors."""


class ConfigError(EventHubError):
    """The configuration is missing, unreadable or invalid."""


class AdapterError(EventHubError):
    """An operating-system adapter failed."""


class CommandError(AdapterError):
    """A subprocess returned a non-zero exit status."""

    def __init__(self, argv: tuple[str, ...], returncode: int, stderr: str) -> None:
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr
        command = " ".join(argv)
        super().__init__(f"command failed ({returncode}): {command}: {stderr.strip()}")


class ServiceError(EventHubError):
    """A service module could not perform an action."""


class DependencyError(EventHubError):
    """A required external component is missing or unsupported."""


class PermissionDeniedError(EventHubError):
    """An operation was rejected by the privilege boundary."""


class NotFoundError(EventHubError):
    """A requested object does not exist."""


class ConflictError(EventHubError):
    """A requested operation conflicts with existing state."""
