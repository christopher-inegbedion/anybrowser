"""``DaemonTransport`` -- how bytes get to the daemon.

The daemon's job is to own a browser engine and serve it. *How* clients reach it
is a separate decision, and a local one: a desktop app wants a Unix socket, a
container wants a TCP WebSocket, a test wants no socket at all. So the server is
written against this interface and never imports a socket library.

Ships with three: ``websocket``, ``unix`` and ``memory``. Add your own with an
entry point in the ``relaykit.transports`` group -- see
:mod:`relaykit.core.registry`.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator, Awaitable, Callable

from .protocol import Message

__all__ = ["Connection", "DaemonTransport", "MessageHandler"]

#: Called once per inbound message. Anything it returns is sent back.
MessageHandler = Callable[["Connection", Message], Awaitable[Message | None]]


class Connection(abc.ABC):
    """One client attached to the daemon."""

    #: Stable per-connection id, used in logs and for event fan-out.
    id: str = ""
    #: Whatever the transport learned about who this is (peer credentials, a
    #: bearer token subject). The server's auth policy reads it; nothing else
    #: should.
    peer: str = ""

    @abc.abstractmethod
    async def send(self, message: Message) -> None:
        """Deliver one message. Must not raise on a closed connection -- drop it."""

    @abc.abstractmethod
    async def close(self) -> None: ...

    @property
    @abc.abstractmethod
    def closed(self) -> bool: ...


class DaemonTransport(abc.ABC):
    """Accepts connections and pumps messages into a handler."""

    #: Registry name. Must match the entry-point key.
    name: str = ""

    @abc.abstractmethod
    async def serve(self, handler: MessageHandler) -> None:
        """Listen until cancelled, calling ``handler`` for every inbound message.

        Must survive one client misbehaving: a malformed frame or a raised
        handler closes that connection and nothing else.
        """

    @abc.abstractmethod
    async def close(self) -> None:
        """Stop listening and close every live connection. Idempotent."""

    @abc.abstractmethod
    def connections(self) -> AsyncIterator[Connection]:
        """Iterate the currently attached clients, for event broadcast."""

    @property
    def address(self) -> str:
        """Human-readable address, for logs and for writing a lockfile."""
        return ""
