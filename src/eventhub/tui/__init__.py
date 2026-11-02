"""Textual terminal UI: a thin presentation layer over the registry/installer."""

from __future__ import annotations

from eventhub.tui.app import EventHubApp, run_tui

__all__ = ["EventHubApp", "run_tui"]
