"""Pytest wiring: engine selection, the fixture server, and capability gating."""

from __future__ import annotations

import asyncio
import functools
import http.server
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from relaykit.core import BrowserEngine, Capability, engines

PAGES = Path(__file__).parent / "pages"


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("relaykit")
    group.addoption(
        "--engine",
        action="store",
        default="fake",
        help="Name of the relaykit engine to test (default: fake).",
    )
    group.addoption(
        "--engine-option",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Constructor option for the engine. Repeatable.",
    )
    group.addoption(
        "--transport",
        action="store",
        default="memory",
        help="Name of the relaykit transport to test (default: memory).",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "requires(*caps): skip unless the engine declares every named capability.",
    )


@pytest.fixture(scope="session")
def engine_options(pytestconfig: pytest.Config) -> dict[str, Any]:
    options: dict[str, Any] = {}
    for raw in pytestconfig.getoption("--engine-option"):
        key, _, value = str(raw).partition("=")
        if not key:
            raise pytest.UsageError(f"malformed --engine-option: {raw!r}")
        options[key.strip()] = _coerce(value)
    return options


def _coerce(value: str) -> Any:
    lowered = value.strip().lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            continue
    return value


@pytest.fixture(scope="session")
def base_url() -> Iterator[str]:
    """Serve the fixture pages over real HTTP.

    Not ``file://`` and not ``data:`` -- both change same-origin behaviour, and
    an engine that only works on them is not an engine.
    """
    handler = functools.partial(_QuietHandler, directory=str(PAGES))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        return


@pytest.fixture(scope="session")
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
def engine(
    pytestconfig: pytest.Config,
    engine_options: dict[str, Any],
    event_loop: asyncio.AbstractEventLoop,
) -> Iterator[BrowserEngine]:
    name = pytestconfig.getoption("--engine")
    try:
        cls = engines.get(name)
    except Exception as exc:
        raise pytest.UsageError(f"cannot load engine {name!r}: {exc}") from exc

    instance = cls(**engine_options)
    event_loop.run_until_complete(instance.start())
    try:
        yield instance
    finally:
        event_loop.run_until_complete(instance.close())


@pytest.fixture(autouse=True)
def _capability_gate(request: pytest.FixtureRequest) -> None:
    marker = request.node.get_closest_marker("requires")
    if marker is None:
        return
    engine_ = request.getfixturevalue("engine")
    declared = engine_.capabilities
    missing = [cap.value for cap in (Capability(c) for c in marker.args) if cap not in declared]
    if missing:
        pytest.skip(f"engine does not declare: {', '.join(missing)}")


@pytest.fixture
def run(event_loop: asyncio.AbstractEventLoop):
    """Run one coroutine on the session loop and return its result."""

    def _run(coro: Any) -> Any:
        return event_loop.run_until_complete(coro)

    return _run


# --------------------------------------------------------------------------- #
# Transport conformance                                                        #
# --------------------------------------------------------------------------- #


@pytest.fixture
def run_transport(event_loop: asyncio.AbstractEventLoop):
    """Serve a transport, hand out clients, and tear it all down.

    The awkward shape -- a callable that is also a context manager factory with
    a ``broadcast`` method -- exists so each test reads as the scenario it is
    testing rather than as six lines of loop plumbing.
    """
    import contextlib

    from relaykit.daemon.protocol import Message, Request, Response

    state: dict[str, Any] = {"transport": None, "task": None, "clients": []}

    class _Client:
        def __init__(self, inner: Any) -> None:
            self._inner = inner
            self._inbox: list[Message] = []

        def send(self, message: Message) -> None:
            event_loop.run_until_complete(self._inner.send(message))

        def send_raw(self, payload: str) -> None:
            event_loop.run_until_complete(self._inner.send_raw(payload))

        def request(self, request: Request, *, allow_error: bool = False) -> Response:
            self.send(request)
            for message in self.drain(expect=1, timeout=5):
                if isinstance(message, Response) and message.id == request.id:
                    if not message.ok and not allow_error:
                        raise AssertionError(f"request failed: {message.error}")
                    return message
            raise AssertionError(f"no response to {request.id}")

        def drain(self, *, expect: int = 0, timeout: float = 1.0) -> list[Message]:
            async def _collect() -> list[Message]:
                out: list[Message] = []
                deadline = asyncio.get_running_loop().time() + timeout
                while expect == 0 or len(out) < expect:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    with contextlib.suppress(Exception):
                        out.append(await self._inner.receive(timeout=remaining))
                        continue
                    break
                return out

            collected = event_loop.run_until_complete(_collect())
            self._inbox.extend(collected)
            return collected

        def wait_closed(self, *, timeout: float = 1.0) -> bool:
            async def _wait() -> bool:
                deadline = asyncio.get_running_loop().time() + timeout
                while asyncio.get_running_loop().time() < deadline:
                    try:
                        await self._inner.receive(timeout=0.1)
                    except Exception:
                        return True
                    await asyncio.sleep(0.02)
                return False

            return event_loop.run_until_complete(_wait())

        def close(self) -> None:
            with contextlib.suppress(Exception):
                event_loop.run_until_complete(self._inner.close())

    @contextlib.contextmanager
    def _serve(transport: Any, handler: Any, *, reuse: bool = False):
        if not reuse:
            state["transport"] = transport
            state["task"] = event_loop.create_task(transport.serve(handler))
            # Let the listener bind before anyone tries to connect.
            event_loop.run_until_complete(asyncio.sleep(0.1))
        client = _Client(
            event_loop.run_until_complete(transport.client_class.connect(transport.address))
        )
        state["clients"].append(client)
        try:
            yield client
        finally:
            client.close()
            if not reuse:
                event_loop.run_until_complete(transport.close())
                task = state.get("task")
                if task is not None:
                    task.cancel()
                    # CancelledError is a BaseException: suppressing Exception
                    # alone lets a routine teardown surface as a test failure.
                    with contextlib.suppress(BaseException):
                        event_loop.run_until_complete(task)
                state["clients"].clear()

    def _broadcast(message: Message) -> None:
        transport = state["transport"]

        async def _fan_out() -> None:
            async for connection in transport.connections():
                await connection.send(message)

        event_loop.run_until_complete(_fan_out())

    _serve.broadcast = _broadcast  # type: ignore[attr-defined]
    return _serve
