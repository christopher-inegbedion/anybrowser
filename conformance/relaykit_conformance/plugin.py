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
