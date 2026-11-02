"""Persisted uplink and network state used by the dashboard and TUI."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from eventhub.core.context import Runtime
from eventhub.network.roles import RoleMap


def state_path(runtime: Runtime) -> Path:
    return runtime.config.state_dir / "network-state.json"


@dataclass(frozen=True, slots=True)
class UplinkState:
    kind: str = "offline"  # wan | wifi | offline
    interface: str = ""
    online: bool = False
    detail: str = ""
    checked_at: float = field(default_factory=time.time)

    @property
    def sharing_available(self) -> bool:
        return self.online and bool(self.interface)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> UplinkState:
        return cls(
            kind=str(payload.get("kind", "offline")),
            interface=str(payload.get("interface", "")),
            online=bool(payload.get("online", False)),
            detail=str(payload.get("detail", "")),
            checked_at=float(payload.get("checked_at", time.time())),
        )


@dataclass(frozen=True, slots=True)
class NetworkState:
    roles: RoleMap
    uplink: UplinkState
    generated_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "roles": self.roles.as_dict(),
            "uplink": self.uplink.as_dict(),
            "generated_at": self.generated_at,
        }


def save_state(runtime: Runtime, state: NetworkState) -> None:
    path = state_path(runtime)
    try:
        runtime.fs.mkdir(path.parent, mode=0o750)
        runtime.fs.atomic_write(
            path, json.dumps(state.as_dict(), indent=2) + "\n", mode=0o644
        )
    except OSError:
        runtime.logger.warning("could not persist network state", exc_info=True)


def load_state(runtime: Runtime) -> NetworkState | None:
    path = state_path(runtime)
    try:
        raw = json.loads(runtime.fs.read_text(path))
        roles = RoleMap(
            ap=str(raw.get("roles", {}).get("ap", "")),
            airdrop=str(raw.get("roles", {}).get("airdrop", "")),
            wan=str(raw.get("roles", {}).get("wan", "")),
            lan=str(raw.get("roles", {}).get("lan", "")),
            wan_kind=str(raw.get("roles", {}).get("wan_kind", "none")),
        )
        return NetworkState(
            roles=roles,
            uplink=UplinkState.from_dict(raw.get("uplink", {})),
            generated_at=float(raw.get("generated_at", time.time())),
        )
    except (OSError, ValueError, TypeError):
        return None
