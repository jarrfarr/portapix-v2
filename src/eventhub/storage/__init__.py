"""Storage module."""

from __future__ import annotations

from eventhub.storage.service import SERVICE, StorageService, systemd_escape_path

__all__ = ["SERVICE", "StorageService", "systemd_escape_path"]
