"""Network package: interface detection, role mapping and uplink control.

The public surface used by the installer/TUI is exported here; the service
module lives in :mod:`eventhub.network.service`.
"""

from __future__ import annotations

from eventhub.network.hardware import HardwareReport, detect_hardware

__all__ = ["HardwareReport", "detect_hardware"]
