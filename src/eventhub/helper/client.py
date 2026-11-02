"""Client for the privileged helper socket (used by the unprivileged dashboard)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType
from typing import Any

from eventhub.core.errors import EventHubError
from eventhub.helper.actions import HelperResponse


class HelperUnavailable(EventHubError):
    pass


class HelperError(EventHubError):
    pass


class HelperClient:
    def __init__(self, socket_path: Path | str, *, timeout: float = 120.0) -> None:
        self.socket_path = Path(socket_path)
        self.timeout = timeout

    async def call(
        self, action: str, payload: Mapping[str, Any] | None = None
    ) -> HelperResponse:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self.socket_path)),
                timeout=min(10.0, self.timeout),
            )
        except (TimeoutError, OSError) as exc:
            raise HelperUnavailable(f"helper socket unavailable: {exc}") from exc
        try:
            request = json.dumps({"action": action, "payload": dict(payload or {})}) + "\n"
            writer.write(request.encode("utf-8"))
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=self.timeout)
            if not line:
                raise HelperError("helper closed the connection without a response")
            try:
                data = json.loads(line)
            except ValueError as exc:
                raise HelperError("helper returned invalid JSON") from exc
            if not isinstance(data, dict):
                raise HelperError("helper returned a non-object response")
            return HelperResponse(
                ok=bool(data.get("ok")),
                message=str(data.get("message", "")),
                data=data.get("data"),
                request_id=str(data.get("request_id", "")),
                elapsed_ms=int(data.get("elapsed_ms", 0) or 0),
            )
        finally:
            writer.close()
            with contextlib.suppress(OSError, ConnectionError):
                await writer.wait_closed()

    def call_sync(
        self, action: str, payload: Mapping[str, Any] | None = None
    ) -> HelperResponse:
        return asyncio.run(self.call(action, payload))

    async def __aenter__(self) -> HelperClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def ping(self) -> bool:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(1.0)
                sock.connect(str(self.socket_path))
            return True
        except OSError:
            return False
