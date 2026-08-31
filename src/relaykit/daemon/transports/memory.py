"""In-process transport. No sockets, no ports, no cleanup.

For tests and for embedding the daemon in the same process as its only client.
It is also the transport to reach for when debugging something else: if a bug
reproduces over ``memory``, it is not a networking bug.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator

from ...core.errors import TransportError
from ..protocol import Message, decode, encode
from ..transport import DaemonClient, MessageHandler
from .base import BaseConnection, BaseTransport

__all__ = ["MemoryClient", "MemoryTransport"]

#: Live servers by address. Module-level because "connect by address" is the
#: interface, and an in-process transport still has to honour it.
_SERVERS: dict[str, MemoryTransport] = {}
_addresses = itertools.count(1)


class _MemoryConnection(BaseConnection):
    def __init__(self, peer: str, outbox: asyncio.Queue[str | None]) -> None:
        super().__init__(peer)
        self._outbox = outbox

    async def _write(self, message: Message) -> None:
        await self._outbox.put(encode(message))

    async def _shutdown(self) -> None:
        await self._outbox.put(None)


class MemoryTransport(BaseTransport):
    name = "memory"
    client_class: type[DaemonClient]

    def __init__(self, address: str = "") -> None:
        super().__init__()
        self._address = address or f"memory://{next(_addresses)}"
        self._handler: MessageHandler | None = None
        self._serving = asyncio.Event()

    @property
    def address(self) -> str:
        return self._address

    async def serve(self, handler: MessageHandler) -> None:
        self._handler = handler
        _SERVERS[self._address] = self
        self._serving.set()
        try:
            await asyncio.Event().wait()  # until cancelled
        finally:
            _SERVERS.pop(self._address, None)
            self._serving.clear()

    async def _stop_listening(self) -> None:
        _SERVERS.pop(self._address, None)
        self._serving.clear()

    # -- called by the client ------------------------------------------ #

    def _attach(self, client: MemoryClient) -> None:
        if self._handler is None:
            raise TransportError("memory transport is not serving", address=self._address)
        connection = _MemoryConnection(f"memory:{client.client_id}", client.inbox)
        client.bind(connection)

        async def _frames() -> AsyncIterator[str]:
            while True:
                raw = await client.outbox.get()
                if raw is None:
                    return
                yield raw

        # Keep a reference: a task nothing holds can be garbage-collected
        # mid-flight, which drops the client's session with no error anywhere.
        task = asyncio.create_task(self._pump(connection, _frames(), self._handler))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


class MemoryClient(DaemonClient):
    _ids = itertools.count(1)

    def __init__(self) -> None:
        self.client_id = f"m{next(MemoryClient._ids)}"
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self.outbox: asyncio.Queue[str | None] = asyncio.Queue()
        self._connection: BaseConnection | None = None

    def bind(self, connection: BaseConnection) -> None:
        self._connection = connection

    @classmethod
    async def connect(cls, address: str, **options: object) -> MemoryClient:
        server = _SERVERS.get(address)
        if server is None:
            raise TransportError("no memory transport at that address", address=address)
        client = cls()
        server._attach(client)
        return client

    async def send(self, message: Message) -> None:
        await self.outbox.put(encode(message))

    async def send_raw(self, payload: str) -> None:
        await self.outbox.put(payload)

    async def receive(self, *, timeout: float = 30.0) -> Message:
        raw = await asyncio.wait_for(self.inbox.get(), timeout)
        if raw is None:
            raise TransportError("connection closed")
        return decode(raw)

    async def close(self) -> None:
        await self.outbox.put(None)
        if self._connection is not None:
            await self._connection.close()


MemoryTransport.client_class = MemoryClient
