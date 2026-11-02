"""Test configuration: cheap argon2 parameters; use /tmp for scratch files."""

from __future__ import annotations

import os

os.environ.setdefault("EVENTHUB_FAST_HASH", "1")
