"""Machinery every transport needs, so none of them reinvents it.

Three things are identical no matter how bytes arrive, and all three are easy to
get subtly wrong:

* **Dispatch.** Each inbound message runs in its own task, so a slow handler
  does not stall the ones behind it. Serialising here is the bug that passes
  every single-request test and corrupts under load.
* **Isolation.** A malformed frame or a raising handler closes at most one
  connection. Everything else keeps serving.
* **Bookkeeping.** A live set of connections, for event broadcast and for
  closing them all on shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from collections.abc import AsyncIterator

from ...core.errors import ProtocolError
from ..protocol import Message, Response
from ..transport import Connection, DaemonTransport, MessageHandler

logger = logging.getLogger(__name__)

__all__ = ["BaseConnection", "BaseTransport"]

_ids = itertools.count(1)


class BaseConnection(Connection):
    """Tracks liveness and swallows sends to a closed peer."""

    def __init__(self, peer: str) -> None:
        self.id = f"c{next(_ids)}"
        self.peer = peer or self.id
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def send(self, message: Message) -> None:
        if self._closed:
            return
        try:
            await self._write(message)
        except Exception:
            # A send that fails means the peer is gone. That is not this
            # connection's problem to raise about -- the daemon is mid-broadcast
            # and the other clients still need theirs.
            logger.debug("dropping message to %s: peer is gone", self.id, exc_info=True)
            self._closed = True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            await self._shutdown()

    # -- subclass hooks ------------------------------------------------ #

    async def _write(self, message: Message) -> None:
        raise NotImplementedError

    async def _shutdown(self) -> None:
        raise NotImplementedError


class BaseTransport(DaemonTransport):
    """Connection bookkeeping and the read loop's error policy."""

    def __init__(self) -> None:
        self._connections: set[BaseConnection] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._closing = False

    async def connections(self) -> AsyncIterator[Connection]:
        # Iterate a copy: a broadcast that closes a dead connection would
        # otherwise mutate the set mid-iteration.
        for connection in tuple(self._connections):
            if not connection.closed:
                yield connection

    def _track(self, connection: BaseConnection) -> None:
        self._connections.add(connection)

    def _forget(self, connection: BaseConnection) -> None:
        self._connections.discard(connection)

    async def _pump(
        self,
        connection: BaseConnection,
        frames: AsyncIterator[str],
        handler: MessageHandler,
    ) -> None:
        """Read frames until the peer goes away, dispatching each concurrently.

        Returning rather than raising on a bad frame is the contract: one client
        sending garbage must not affect any other.
        """
        from ..protocol import decode

        self._track(connection)
        try:
            async for raw in frames:
                try:
                    message = decode(raw)
                except ProtocolError as exc:
                    logger.info("closing %s: %s", connection.id, exc)
                    return
                task = asyncio.create_task(self._dispatch(connection, message, handler))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self._forget(connection)
            await connection.close()

    async def _dispatch(
        self, connection: BaseConnection, message: Message, handler: MessageHandler
    ) -> None:
        """Run one handler. A handler that raises answers with an error, not a crash."""
        try:
            reply = await handler(connection, message)
        except Exception as exc:
            logger.exception("handler raised on %s", connection.id)
            reply = Response(
                id=getattr(message, "id", ""),
                ok=False,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            if not reply.id:
                return
        if reply is not None:
            await connection.send(reply)

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        for task in tuple(self._tasks):
            task.cancel()
        for connection in tuple(self._connections):
            await connection.close()
        self._connections.clear()
        await self._stop_listening()

    async def _stop_listening(self) -> None:
        raise NotImplementedError
