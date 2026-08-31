# Writing a transport

A transport decides how clients reach the daemon. The daemon itself never
imports a socket library — it is handed connections and messages.

Implement two methods:

```python
from relaykit.daemon.transport import DaemonTransport, Connection, MessageHandler


class GrpcTransport(DaemonTransport):
    name = "grpc"

    async def serve(self, handler: MessageHandler) -> None:
        """Listen until cancelled, calling handler for every inbound message."""

    async def close(self) -> None:
        """Stop listening, close every live connection. Idempotent."""
```

```toml
[project.entry-points."relaykit.transports"]
grpc = "my_package.transport:GrpcTransport"
```

## The contract

**One message in, at most one message out.** Requests carry an `id` and get
exactly one response. Events carry no `id` and are never acknowledged. Do not
invent a third shape.

**Frame it yourself.** The protocol is JSON objects with no embedded newlines
([`protocol.py`](../../src/relaykit/daemon/protocol.py)). How they are delimited
is the transport's business — WebSocket messages, newline-delimited on a stream,
length-prefixed. Decode with `protocol.decode`, which raises `ProtocolError`
rather than handing the server a half-understood message.

**One bad client must not take down the daemon.** A malformed frame or a raised
handler closes *that* connection. Everything else keeps running. This is the
single most common transport bug and it is invisible until the day it matters.

**Populate `Connection.peer`.** Whatever you learned about who this is — peer
credentials on a Unix socket, a bearer token subject on WebSocket. The auth
policy reads it; nothing else should.

**Never bind non-loopback by default.** The daemon is complete control of a
browser holding the user's live sessions. A transport that defaults to
`0.0.0.0` is a vulnerability, not a convenience.

## Testing

Point the daemon test suite at yours:

```bash
pytest --pyargs relaykit.daemon.tests --transport grpc
```

It checks framing, concurrent requests, event fan-out, one-client-fails
isolation, and clean shutdown under load.
