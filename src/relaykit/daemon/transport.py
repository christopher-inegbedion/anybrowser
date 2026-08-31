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

__all__ = ["Connection", "DaemonClient", "DaemonTransport", "MessageHandler"]

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

    #: The matching client implementation. Required -- see :class:`DaemonClient`.
    client_class: type[DaemonClient]

    @property
    def address(self) -> str:
        """Where a client should connect. Also what gets written to a lockfile."""
        return ""


class DaemonClient(abc.ABC):
    """The other end of a transport.

    A transport that ships only a server is half an interface: nothing can talk
    to it without reimplementing its framing, and the conformance suite cannot
    exercise it generically. So every transport ships both, and the pair is what
    gets tested.

        client = await UnixSocketTransport.client_class.connect(address)
        await client.send(Request(method="engine.url"))
        reply = await client.receive()
    """

    @classmethod
    @abc.abstractmethod
    async def connect(cls, address: str, **options: object) -> DaemonClient:
        """Open a connection to a daemon listening at ``address``."""

    @abc.abstractmethod
    async def send(self, message: Message) -> None: ...

    @abc.abstractmethod
    async def receive(self, *, timeout: float = 30.0) -> Message:
        """Return the next inbound message, or raise ``TimeoutError``.

        Responses and events arrive on the same stream and in no guaranteed
        order relative to each other -- an event may land between a request and
        its response. Callers correlate by ``Request.id``; they must not assume
        the next message is theirs.
        """

    @abc.abstractmethod
    async def close(self) -> None: ...

    async def send_raw(self, payload: str) -> None:
        """Send an unvalidated frame. Exists for testing malformed input."""
        raise NotImplementedError
