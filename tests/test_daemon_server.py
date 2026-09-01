"""Daemon behaviour the engine contract cannot reach.

Running the engine contract through the daemon already proves the happy path
end to end (``--via-daemon``). What it cannot show is what happens when a client
misbehaves, when auth refuses, or when the far end raises: those need a daemon
that is not simply passing traffic.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from relaykit.core.engine import BrowserEngine, Capabilities, Capability, EngineInfo
from relaykit.core.errors import (
    CapabilityNotSupported,
    PermissionDenied,
    StaleHandle,
)
from relaykit.core.types import ActionOutcome, NavigationResult, Screenshot, Snapshot, Viewport
from relaykit.daemon import DaemonServer, Event, RemoteEngine, TokenAuth
from relaykit.daemon.transports.memory import MemoryTransport


class _StubEngine(BrowserEngine):
    """An engine that raises on demand, so the client's error handling is testable."""

    name = "stub"

    def __init__(self) -> None:
        self.raise_next: Exception | None = None

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities.of(Capability.EVALUATE_JS, evaluate_js="stubbed")

    async def info(self) -> EngineInfo:
        return EngineInfo(name=self.name, browser="stub", browser_version="1.0")

    async def start(self) -> None: ...
    async def close(self) -> None: ...

    async def url(self) -> str:
        if self.raise_next is not None:
            error, self.raise_next = self.raise_next, None
            raise error
        return "https://stub.invalid/"

    async def title(self) -> str:
        return "stub"

    async def viewport(self) -> Viewport:
        return Viewport(width=800, height=600)

    async def snapshot(self, *, include_text: bool = True) -> Snapshot:
        return Snapshot(url=await self.url(), title="stub", viewport=await self.viewport())

    async def screenshot(self, *, full_page: bool = False, clip=None) -> Screenshot:
        return Screenshot(data=b"\x89PNG\r\n\x1a\n", width=800, height=600)

    async def navigate(self, url: str, *, timeout: float = 30.0) -> NavigationResult:
        return NavigationResult(url=url)

    async def reload(self, *, timeout: float = 30.0) -> NavigationResult:
        return NavigationResult(url="https://stub.invalid/")

    async def go_back(self, *, timeout: float = 30.0) -> NavigationResult:
        return NavigationResult(url="https://stub.invalid/")

    async def click(self, target, **kwargs) -> ActionOutcome:
        return ActionOutcome.no_change("stub never clicks")

    async def type_text(self, text: str, **kwargs) -> ActionOutcome:
        return ActionOutcome.no_change("stub never types")

    async def press_key(self, key: str, **kwargs) -> ActionOutcome:
        return ActionOutcome.no_change("stub has no keyboard")

    async def scroll(self, delta_x: float, delta_y: float, **kwargs) -> ActionOutcome:
        return ActionOutcome.no_change("stub has nothing to scroll")

    async def evaluate(self, script: str, *args, frame_id: str = ""):
        return {"echoed": script, "args": list(args)}


async def _connected(engine: BrowserEngine, **server_kwargs):
    transport = MemoryTransport()
    server = DaemonServer(engine, transport, **server_kwargs)
    task = asyncio.create_task(server.serve())
    await asyncio.sleep(0.05)
    return server, transport, task


async def _teardown(server, task, *clients) -> None:
    for client in clients:
        await client.close()
    await server.close()
    task.cancel()
    with contextlib.suppress(BaseException):
        await task


async def test_capabilities_are_adopted_from_the_far_end():
    """A caller routing on capabilities must get the same answer either side."""
    stub = _StubEngine()
    server, transport, task = await _connected(stub)
    remote = RemoteEngine(transport="memory", address=transport.address)
    await remote.start()
    try:
        assert Capability.EVALUATE_JS in remote.capabilities
        assert Capability.SCREENCAST not in remote.capabilities
        assert remote.capabilities.notes[Capability.EVALUATE_JS] == "stubbed"
    finally:
        await _teardown(server, task, remote)


async def test_remote_reports_itself_not_the_far_end():
    """`name` must stay "remote": a socket is not a local browser."""
    stub = _StubEngine()
    server, transport, task = await _connected(stub)
    remote = RemoteEngine(transport="memory", address=transport.address)
    await remote.start()
    try:
        info = await remote.info()
        assert info.name == "remote"
        assert info.browser == "stub"
        assert info.detail["remote_engine"] == "stub"
    finally:
        await _teardown(server, task, remote)


async def test_error_class_survives_the_wire():
    """The hierarchy is how callers decide what to do; flattening it loses that.

    StaleHandle means re-snapshot, CapabilityNotSupported means never. A daemon
    that returned both as a generic error would make every client guess.
    """
    stub = _StubEngine()
    server, transport, task = await _connected(stub)
    remote = RemoteEngine(transport="memory", address=transport.address)
    await remote.start()
    try:
        stub.raise_next = StaleHandle("gone", handle="1:2")
        with pytest.raises(StaleHandle):
            await remote.url()

        stub.raise_next = CapabilityNotSupported("nope")
        with pytest.raises(CapabilityNotSupported):
            await remote.url()
    finally:
        await _teardown(server, task, remote)


async def test_unknown_method_is_refused_not_fatal():
    stub = _StubEngine()
    server, transport, task = await _connected(stub)
    remote = RemoteEngine(transport="memory", address=transport.address)
    await remote.start()
    try:
        with pytest.raises(Exception, match="unknown method"):
            await remote._call("engine.does_not_exist")
        # The connection must still work afterwards.
        assert await remote.title() == "stub"
    finally:
        await _teardown(server, task, remote)


async def test_bad_parameters_are_refused_not_fatal():
    stub = _StubEngine()
    server, transport, task = await _connected(stub)
    remote = RemoteEngine(transport="memory", address=transport.address)
    await remote.start()
    try:
        with pytest.raises(Exception, match="bad parameters"):
            await remote._call("engine.navigate", {"nonsense": 1})
        assert await remote.title() == "stub"
    finally:
        await _teardown(server, task, remote)


async def test_token_auth_refuses_before_hello():
    stub = _StubEngine()
    server, transport, task = await _connected(stub, auth=TokenAuth("s3cret"))
    wrong = RemoteEngine(transport="memory", address=transport.address, token="wrong")
    try:
        with pytest.raises(PermissionDenied):
            await wrong.start()
    finally:
        await wrong.close()

    right = RemoteEngine(transport="memory", address=transport.address, token="s3cret")
    await right.start()
    try:
        assert await right.title() == "stub"
    finally:
        await _teardown(server, task, right)


async def test_events_reach_every_client():
    stub = _StubEngine()
    server, transport, task = await _connected(stub)
    a = RemoteEngine(transport="memory", address=transport.address)
    b = RemoteEngine(transport="memory", address=transport.address)
    await a.start()
    await b.start()
    try:
        await server.broadcast(Event(event="tab.navigated", data={"url": "u"}))
        for client in (a, b):
            event = await client.next_event(timeout=2)
            assert event.event == "tab.navigated"
            assert event.data["url"] == "u"
    finally:
        await _teardown(server, task, a, b)


async def test_screenshot_bytes_survive_the_wire():
    """Binary through JSON is the classic corruption; base64 keeps it exact."""
    stub = _StubEngine()
    server, transport, task = await _connected(stub)
    remote = RemoteEngine(transport="memory", address=transport.address)
    await remote.start()
    try:
        shot = await remote.screenshot()
        assert shot.data == b"\x89PNG\r\n\x1a\n"
    finally:
        await _teardown(server, task, remote)


async def test_evaluate_round_trips_arguments():
    stub = _StubEngine()
    server, transport, task = await _connected(stub)
    remote = RemoteEngine(transport="memory", address=transport.address)
    await remote.start()
    try:
        result = await remote.evaluate("1+1", 5, "x")
        assert result == {"echoed": "1+1", "args": [5, "x"]}
    finally:
        await _teardown(server, task, remote)
