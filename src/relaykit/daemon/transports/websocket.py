"""WebSocket transport, for reaching the daemon from another process or a container.

Use this when a Unix socket will not reach — a browser extension, a UI in a
different sandbox, a daemon in a container. Otherwise prefer ``unix``: it gets
access control from the filesystem, and this does not.

**It binds loopback and it must stay that way.** The daemon is complete control
of a browser holding the user's live sessions. Binding it to a routable address
without deliberate, authenticated intent is a vulnerability rather than a
convenience, which is why ``host`` defaults to 127.0.0.1 and there is no config
flag that quietly widens it.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

from ...core.errors import TransportError
from ..protocol import Message, decode, encode
from ..transport import DaemonClient, MessageHandler
from .base import BaseConnection, BaseTransport

__all__ = ["WebSocketClient", "WebSocketTransport"]


def _websockets() -> Any:
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover - depends on the install extra
        raise TransportError(
            "the websocket transport needs the 'websockets' package; pip install 'relaykit[daemon]'"
        ) from exc
    return websockets


class _WebSocketConnection(BaseConnection):
    def __init__(self, socket: Any, peer: str) -> None:
        super().__init__(peer)
        self._socket = socket

    async def _write(self, message: Message) -> None:
        await self._socket.send(encode(message))

    async def _shutdown(self) -> None:
        await self._socket.close()

    async def frames(self) -> AsyncIterator[str]:
        try:
            async for raw in self._socket:
                yield raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
        except Exception:
            return


class WebSocketTransport(BaseTransport):
    name = "websocket"
    client_class: type[DaemonClient]

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        super().__init__()
        self._host = host
        self._port = port
        self._server: Any = None
        self._bound_port = 0

    @property
    def address(self) -> str:
        return f"ws://{self._host}:{self._bound_port or self._port}"

    async def serve(self, handler: MessageHandler) -> None:
        websockets = _websockets()

        async def _on_client(socket: Any) -> None:
            remote = socket.remote_address or ()
            peer = ":".join(str(part) for part in remote[:2]) or "ws"
            connection = _WebSocketConnection(socket, peer)
            await self._pump(connection, connection.frames(), handler)

        self._server = await websockets.serve(_on_client, self._host, self._port)
        # Ephemeral ports are only knowable after bind, and `address` is what
        # clients and lockfiles are handed.
        for sock in self._server.sockets or ():
            self._bound_port = sock.getsockname()[1]
            break
        try:
            await asyncio.Future()  # until cancelled
        finally:
            await self._stop_listening()

    async def _stop_listening(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None


class WebSocketClient(DaemonClient):
    def __init__(self, socket: Any) -> None:
        self._socket = socket

    @classmethod
    async def connect(cls, address: str, **options: object) -> WebSocketClient:
        websockets = _websockets()
        try:
            socket = await websockets.connect(address)
        except Exception as exc:
            raise TransportError(f"cannot connect to {address}: {exc}") from exc
        return cls(socket)

    async def send(self, message: Message) -> None:
        await self.send_raw(encode(message))

    async def send_raw(self, payload: str) -> None:
        await self._socket.send(payload)

    async def receive(self, *, timeout: float = 30.0) -> Message:
        try:
            raw = await asyncio.wait_for(self._socket.recv(), timeout)
        except asyncio.TimeoutError:
            raise
        except Exception as exc:
            raise TransportError(f"connection closed: {exc}") from exc
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return decode(raw)

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._socket.close()


WebSocketTransport.client_class = WebSocketClient
