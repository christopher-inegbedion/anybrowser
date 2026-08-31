"""RelayKit -- a pluggable browser-automation and agent runtime.

Three interfaces, each with an entry-point registry, each with a conformance
suite you can run against your own implementation:

* :class:`relaykit.core.BrowserEngine` -- drive a browser (Chrome, Safari, yours)
* :class:`relaykit.daemon.DaemonTransport` -- how clients reach the daemon
* :class:`relaykit.models.ModelProvider` -- where completions come from

    from relaykit import open_engine

    async with await open_engine("chrome") as engine:
        await engine.navigate("https://example.com")
        page = await engine.snapshot()
        print(page.title, len(page.elements), "elements")
"""

from __future__ import annotations

from typing import Any

from .core import BrowserEngine, Capability, SyncEngine, engines
from .core.errors import RelayKitError

__version__ = "0.1.0"

__all__ = [
    "BrowserEngine",
    "Capability",
    "RelayKitError",
    "SyncEngine",
    "__version__",
    "available_engines",
    "engines",
    "open_engine",
]


async def open_engine(name: str, /, **options: Any) -> BrowserEngine:
    """Construct and start the named engine.

    ``name`` is a key in the ``relaykit.engines`` registry. The returned engine
    is already started and is also an async context manager, so both of these
    work::

        engine = await open_engine("chrome")
        async with await open_engine("chrome") as engine: ...
    """
    cls = engines.get(name)
    await cls.probe()
    engine = cls(**options)
    await engine.start()
    return engine


def available_engines() -> list[str]:
    """Every registered engine name, installed plugins included."""
    return engines.names()
