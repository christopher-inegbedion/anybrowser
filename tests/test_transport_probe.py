"""`DaemonTransport.probe()` — unavailable here is not broken.

Regression cover for the bug a clean-virtualenv install found: the websocket
transport failed eight contract tests because its optional dependency was
absent. See ADR-0001.
"""

from __future__ import annotations

import socket
import sys

import pytest

from relaykit.core.errors import TransportError
from relaykit.daemon.transports.memory import MemoryTransport
from relaykit.daemon.transports.unix import UnixSocketTransport
from relaykit.daemon.transports.websocket import WebSocketTransport


async def test_a_transport_with_nothing_to_check_is_available():
    """The default is a no-op, so a third-party transport need not opt in."""
    assert await MemoryTransport.probe() is None


async def test_websocket_refuses_without_its_extra(monkeypatch):
    """None in sys.modules makes `import websockets` raise, as a missing install does."""
    monkeypatch.setitem(sys.modules, "websockets", None)
    with pytest.raises(TransportError) as excinfo:
        await WebSocketTransport.probe()
    # The message has to name the extra, or the skip it produces is a dead end.
    assert "relaykit[daemon]" in str(excinfo.value)


async def test_websocket_is_available_when_the_extra_is_there():
    pytest.importorskip("websockets")
    assert await WebSocketTransport.probe() is None


async def test_unix_refuses_where_the_platform_has_no_af_unix(monkeypatch):
    monkeypatch.delattr(socket, "AF_UNIX", raising=False)
    with pytest.raises(TransportError) as excinfo:
        await UnixSocketTransport.probe()
    assert "websocket" in str(excinfo.value)  # points at the way out


async def test_unix_is_available_on_a_posix_host():
    pytest.importorskip("socket")
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("no AF_UNIX on this platform")
    assert await UnixSocketTransport.probe() is None
