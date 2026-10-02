"""The daemon: one browser, many clients.

:class:`DaemonServer` owns an engine and serves it over a transport.
:class:`RemoteEngine` is the other end -- and is itself a ``BrowserEngine``,
which is what lets the engine conformance suite grade the whole daemon stack.
"""

from .client import RemoteEngine
from .protocol import PROTOCOL_VERSION, Event, Request, Response, decode, encode
from .server import AllowAll, AuthPolicy, DaemonServer, TokenAuth, serve_forever
from .transport import Connection, DaemonClient, DaemonTransport

__all__ = [
    "PROTOCOL_VERSION",
    "AllowAll",
    "AuthPolicy",
    "Connection",
    "DaemonClient",
    "DaemonServer",
    "DaemonTransport",
    "Event",
    "RemoteEngine",
    "Request",
    "Response",
    "TokenAuth",
    "decode",
    "encode",
    "serve_forever",
]
