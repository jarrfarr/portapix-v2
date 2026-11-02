"""Idempotent installation, update, repair, backup and uninstall."""

from __future__ import annotations

from eventhub.installer.backup import BackupInfo, BackupManager
from eventhub.installer.manager import Installer, InstallResult, UninstallSummary
from eventhub.installer.steps import InstallContext, InstallOptions, StepResult

__all__ = [
    "BackupInfo",
    "BackupManager",
    "InstallContext",
    "InstallOptions",
    "InstallResult",
    "Installer",
    "StepResult",
    "UninstallSummary",
]
