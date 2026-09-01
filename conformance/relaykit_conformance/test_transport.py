"""The transport contract.

    pytest --pyargs relaykit_conformance --transport unix

A transport carries framed messages between clients and the daemon and knows
nothing else. These tests are what "correctly" means -- especially the last two,
which are the bugs that stay invisible until the day they matter.
"""

from __future__ import annotations

import asyncio

import pytest

from relaykit.core.errors import ProtocolError
from relaykit.daemon.protocol import Event, Request, Response, decode, encode


@pytest.fixture
def transport_name(pytestconfig: pytest.Config) -> str:
    name = pytestconfig.getoption("--transport")
    if not name:
        pytest.skip("no --transport given")
    return str(name)


@pytest.fixture
def transport(transport_name: str):
    from relaykit.core.registry import transports

    return transports.get(transport_name)()


def test_registered_name_matches(transport, transport_name):
    assert transport.name == transport_name


def test_request_response_round_trip(transport, run_transport):
    """One request in, exactly one response out, carrying the same id."""

    async def handler(connection, message):
        assert isinstance(message, Request)
        return Response(id=message.id, result={"echo": message.params})

    with run_transport(transport, handler) as client:
        reply = client.request(Request(method="ping", params={"n": 1}, id="abc"))
        assert isinstance(reply, Response)
        assert reply.id == "abc" and reply.ok
        assert reply.result == {"echo": {"n": 1}}


def test_events_are_not_acknowledged(transport, run_transport):
    """An event has no id and gets no reply. Sending one back is a protocol bug."""

    async def handler(connection, message):
        return None

    with run_transport(transport, handler) as client:
        client.send(Event(event="client.hello"))
        assert client.drain(timeout=0.3) == []


def test_concurrent_requests_do_not_interleave(transport, run_transport):
    """Slow handlers must not reorder or merge replies.

    A transport that writes responses without framing them per-request passes
    every single-request test and corrupts under any real load.
    """

    async def handler(connection, message):
        await asyncio.sleep(0.05 if message.params.get("slow") else 0)
        return Response(id=message.id, result=message.params.get("n"))

    with run_transport(transport, handler) as client:
        sent = [
            Request(method="x", params={"n": n, "slow": n % 2 == 0}, id=str(n)) for n in range(10)
        ]
        for request in sent:
            client.send(request)
        replies = client.drain(expect=10, timeout=5)
        assert {r.id: r.result for r in replies} == {str(n): n for n in range(10)}


def test_events_broadcast_to_every_connection(transport, run_transport):
    async def handler(connection, message):
        return None

    with (
        run_transport(transport, handler) as client_a,
        run_transport(transport, handler, reuse=True) as client_b,
    ):
        run_transport.broadcast(Event(event="tab.navigated", data={"url": "u"}))
        for client in (client_a, client_b):
            events = client.drain(expect=1, timeout=2)
            assert events[0].event == "tab.navigated"


def test_a_malformed_frame_closes_only_that_connection(transport, run_transport):
    """The single most common transport bug.

    One client sending garbage must not take the daemon, or anyone else's
    session, down with it.
    """

    async def handler(connection, message):
        return Response(id=message.id, result="ok")

    with (
        run_transport(transport, handler) as victim,
        run_transport(transport, handler, reuse=True) as bystander,
    ):
        victim.send_raw("{not json")
        assert victim.wait_closed(timeout=2)
        reply = bystander.request(Request(method="ping"))
        assert reply.ok, "a bad client took down a good one"


def test_a_raising_handler_does_not_kill_the_server(transport, run_transport):
    async def handler(connection, message):
        if message.params.get("boom"):
            raise RuntimeError("handler exploded")
        return Response(id=message.id, result="ok")

    with run_transport(transport, handler) as client:
        client.request(Request(method="x", params={"boom": True}), allow_error=True)
        assert client.request(Request(method="x")).ok


def test_peer_is_populated(transport, run_transport):
    """Whatever the transport learned about who this is. The auth policy reads it."""
    seen: list[str] = []

    async def handler(connection, message):
        seen.append(connection.peer)
        return Response(id=message.id, result=None)

    with run_transport(transport, handler) as client:
        client.request(Request(method="ping"))
    assert seen and seen[0] != "", "Connection.peer was never set"


def test_close_is_idempotent(transport, run_transport):
    async def handler(connection, message):
        return None

    with run_transport(transport, handler):
        pass
    asyncio.run(transport.close())
    asyncio.run(transport.close())


def test_decode_rejects_garbage():
    for raw in ("{not json", "[]", '"str"', "{}"):
        with pytest.raises(ProtocolError):
            decode(raw)
    assert "\n" not in encode(Event(event="e", data={"t": "a\nb"}))
