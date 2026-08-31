"""Unix domain socket transport. The default for a daemon on the user's machine.

Preferred over a TCP port for one reason that matters: filesystem permissions
are real access control. A socket at mode 0600 in the user's own runtime
directory cannot be reached by another user or by anything on the network, which
is the right default for a service holding complete control of a browser full of
live sessions.

Framing is newline-delimited JSON, which the protocol guarantees is safe --
``encode`` never emits an embedded newline.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import struct
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

from ...core.errors import TransportError
from ..protocol import Message, decode, encode
from ..transport import DaemonClient, MessageHandler
from .base import BaseConnection, BaseTransport

__all__ = ["UnixSocketClient", "UnixSocketTransport"]


def _peer_identity(sock: socket.socket | None, fallback: str) -> str:
    """Who is on the other end, as far as the kernel will say.

    Peer credentials are the only identity a local socket can offer that the
    client cannot forge, so the auth policy gets those where the platform has
    them and a synthetic id where it does not.
    """
    if sock is None:
        return fallback
    with contextlib.suppress(Exception):
        if hasattr(socket, "SO_PEERCRED"):  # Linux
            raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            pid, uid, _gid = struct.unpack("3i", raw)
            return f"pid={pid} uid={uid}"
        if hasattr(socket, "LOCAL_PEERCRED"):  # macOS and BSDs
            raw = sock.getsockopt(0, 0x001, struct.calcsize("IIIIi"))
            _version, uid, *_rest = struct.unpack("IIIIi", raw)
            return f"uid={uid}"
    return fallback


class _StreamConnection(BaseConnection):
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, peer: str):
        super().__init__(peer)
        self._reader = reader
        self._writer = writer
        self._lock = asyncio.Lock()

    async def _write(self, message: Message) -> None:
        # One writer at a time: two concurrent handlers writing interleaved
        # bytes is how responses get corrupted under load.
        async with self._lock:
            self._writer.write(encode(message).encode("utf-8") + b"\n")
            await self._writer.drain()

    async def _shutdown(self) -> None:
        self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()

    async def frames(self) -> AsyncIterator[str]:
        while True:
            line = await self._reader.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                yield text


class UnixSocketTransport(BaseTransport):
    name = "unix"
    client_class: type[DaemonClient]

    def __init__(self, path: str = "", *, mode: int = 0o600) -> None:
        super().__init__()
        self._path = (
            Path(path) if path else Path(tempfile.mkdtemp(prefix="relaykit-")) / "daemon.sock"
        )
        self._mode = mode
        self._server: asyncio.AbstractServer | None = None

    @property
    def address(self) -> str:
        return str(self._path)

    async def serve(self, handler: MessageHandler) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # A stale socket from a crashed daemon makes bind fail with EADDRINUSE
        # even though nothing is listening. Removing it is safe: a live daemon
        # would still hold the path and we would fail on connect instead.
        with contextlib.suppress(FileNotFoundError):
            self._path.unlink()

        async def _on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            sock = writer.get_extra_info("socket")
            connection = _StreamConnection(reader, writer, "")
            connection.peer = _peer_identity(sock, f"unix:{connection.id}")
            await self._pump(connection, connection.frames(), handler)

        self._server = await asyncio.start_unix_server(_on_client, path=str(self._path))
        os.chmod(self._path, self._mode)
        try:
            await self._server.serve_forever()
        except asyncio.CancelledError:
            raise
        finally:
            await self._stop_listening()

    async def _stop_listening(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        with contextlib.suppress(FileNotFoundError):
            self._path.unlink()


class UnixSocketClient(DaemonClient):
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    @classmethod
    async def connect(cls, address: str, **options: object) -> UnixSocketClient:
        try:
            reader, writer = await asyncio.open_unix_connection(address)
        except OSError as exc:
            raise TransportError(f"cannot connect to {address}: {exc}") from exc
        return cls(reader, writer)

    async def send(self, message: Message) -> None:
        await self.send_raw(encode(message))

    async def send_raw(self, payload: str) -> None:
        self._writer.write(payload.encode("utf-8") + b"\n")
        await self._writer.drain()

    async def receive(self, *, timeout: float = 30.0) -> Message:
        line = await asyncio.wait_for(self._reader.readline(), timeout)
        if not line:
            raise TransportError("connection closed")
        return decode(line.decode("utf-8").strip())

    async def close(self) -> None:
        self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()


UnixSocketTransport.client_class = UnixSocketClient
