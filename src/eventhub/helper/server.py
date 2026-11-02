"""Unix-socket server for the privileged helper."""

from __future__ import annotations

import asyncio
import contextlib
import grp
import json
import os
import pwd
import socket
import struct
from collections.abc import Callable
from typing import Any

from eventhub.core.context import Runtime
from eventhub.core.errors import ConfigError
from eventhub.helper.actions import ActionDispatcher, HelperResponse

PeerChecker = Callable[[Any], bool]


def _read_peer(sock: socket.socket) -> tuple[int, int, int]:
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, gid = struct.unpack("3i", raw)
    return pid, uid, gid


def make_peer_checker(runtime: Runtime) -> PeerChecker:
    """Allow root, the dashboard user, or a member of the socket group."""

    cfg = runtime.config.helper
    allowed_gid: int | None
    try:
        allowed_gid = grp.getgrnam(cfg.socket_group).gr_gid
    except KeyError:
        allowed_gid = None
    allowed_uids: set[int] = set()
    for name in (runtime.config.install.dashboard_user, runtime.config.install.service_user):
        try:
            allowed_uids.add(pwd.getpwnam(name).pw_uid)
        except KeyError:
            continue

    def check(sock: Any) -> bool:
        try:
            _pid, uid, gid = _read_peer(sock)
        except OSError:
            return False
        if uid == 0:
            return True
        if uid in allowed_uids:
            return True
        return allowed_gid is not None and gid == allowed_gid

    return check


class HelperServer:
    def __init__(
        self,
        runtime: Runtime,
        *,
        peer_checker: PeerChecker | None = None,
    ) -> None:
        self.runtime = runtime
        self.dispatcher = ActionDispatcher(runtime)
        self.peer_checker = peer_checker or make_peer_checker(runtime)
        self.log = runtime.logger.getChild("helper.server")

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        cfg = self.runtime.config.helper
        sock = writer.get_extra_info("socket")
        if sock is None or not hasattr(sock, "getsockopt") or not self.peer_checker(sock):
            self.log.warning("rejected helper connection from a disallowed peer")
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        try:
            while True:
                try:
                    line = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    await self._send(writer, HelperResponse(False, "request too large"))
                    break
                if not line:
                    break
                if len(line) > cfg.max_request_bytes:
                    response = HelperResponse(False, "request too large")
                    await self._send(writer, response)
                    break
                try:
                    request = json.loads(line)
                except ValueError:
                    await self._send(writer, HelperResponse(False, "invalid JSON"))
                    continue
                if not isinstance(request, dict):
                    await self._send(writer, HelperResponse(False, "request must be an object"))
                    continue
                action = str(request.get("action", ""))
                payload = request.get("payload") or {}
                if not isinstance(payload, dict):
                    await self._send(writer, HelperResponse(False, "payload must be an object"))
                    continue
                request_id = str(request.get("request_id", ""))
                remote = ""
                with contextlib.suppress(Exception):
                    _pid, uid, _gid = _read_peer(sock)
                    remote = f"uid:{uid}"
                try:
                    response = await asyncio.wait_for(
                        asyncio.to_thread(
                            self.dispatcher.dispatch, action, payload, remote=remote
                        ),
                        timeout=cfg.request_timeout,
                    )
                except TimeoutError:
                    response = HelperResponse(False, f"action timed out: {action}")
                response = HelperResponse(
                    response.ok, response.message, response.data, request_id, response.elapsed_ms
                )
                await self._send(writer, response)
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _send(self, writer: asyncio.StreamWriter, response: HelperResponse) -> None:
        writer.write((json.dumps(response.as_dict()) + "\n").encode("utf-8"))
        await writer.drain()

    async def serve_forever(self) -> None:
        cfg = self.runtime.config.helper
        path = cfg.socket_path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        server = await asyncio.start_unix_server(
            self.handle, path=str(path), limit=cfg.max_request_bytes + 1
        )
        try:
            try:
                gid = grp.getgrnam(cfg.socket_group).gr_gid
                os.chown(path.parent, 0, gid)
                os.chmod(path.parent, 0o750)
                os.chown(path, 0, gid)
            except KeyError:
                self.log.warning("socket group %s does not exist", cfg.socket_group)
            os.chmod(path, cfg.socket_mode)
            self.log.info("helper listening on %s", path)
            await server.serve_forever()
        finally:
            server.close()
            await server.wait_closed()
            with contextlib.suppress(FileNotFoundError):
                path.unlink()


def run_helper(runtime: Runtime) -> None:
    if not runtime.privileged:
        raise ConfigError("the helper must run as root")
    configure_logging(runtime)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(HelperServer(runtime).serve_forever())


def configure_logging(runtime: Runtime) -> None:
    from eventhub.core.logging import configure_logging as setup

    setup(
        level=runtime.config.log_level,
        log_dir=runtime.config.log_dir,
        filename="helper.log",
    )
