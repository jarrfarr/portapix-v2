"""System module: metrics, recovery, self-test, power and updates."""

from __future__ import annotations

from eventhub.system.recovery import RecoveryManager
from eventhub.system.selftest import SelfTest, SelfTestReport
from eventhub.system.service import SERVICE, SystemService

__all__ = [
    "SERVICE",
    "RecoveryManager",
    "SelfTest",
    "SelfTestReport",
    "SystemService",
]
