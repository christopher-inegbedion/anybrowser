"""AnyBrowser -- a pluggable browser-automation and agent runtime.

Three interfaces, each with an entry-point registry, each with a conformance
suite you can run against your own implementation:

* :class:`anybrowser.core.BrowserEngine` -- drive a browser (Chrome, Safari, yours)
* :class:`anybrowser.daemon.DaemonTransport` -- how clients reach the daemon
* :class:`anybrowser.models.ModelProvider` -- where completions come from

    from anybrowser import open_engine

    async with await open_engine("chrome") as engine:
        await engine.navigate("https://example.com")
        page = await engine.snapshot()
        print(page.title, len(page.elements), "elements")
"""

from __future__ import annotations

from typing import Any

# NOT `from .core import engines`: the `anybrowser.engines` SUBPACKAGE binds
# that same name on this module as soon as anything imports it, silently
# replacing the registry with the package. In a source tree it survives on
# import-order luck; from an installed wheel it does not. Import the
# registry under a name nothing else claims.
from .core import BrowserEngine, Capability, SyncEngine
from .core.errors import AnyBrowserError
from .core.registry import engines as engine_registry


def _installed_version() -> str:
    """The version from package metadata, so there is one source of truth.

    A literal here is a second one, and the two diverge the moment a release
    bumps `pyproject.toml` and forgets this file -- which happened: a 0.2.0
    wheel whose CLI reported 0.1.0, caught by installing it rather than by
    reading it. Metadata is absent when running from an uninstalled source
    tree, and "0.0.0+unknown" is a more honest answer there than a number that
    looks real.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("anybrowser")
    except PackageNotFoundError:
        return "0.0.0+unknown"


__version__ = _installed_version()

__all__ = [
    "AnyBrowserError",
    "BrowserEngine",
    "Capability",
    "SyncEngine",
    "__version__",
    "available_engines",
    "engines",
    "open_engine",
]


async def open_engine(name: str, /, **options: Any) -> BrowserEngine:
    """Construct and start the named engine.

    ``name`` is a key in the ``anybrowser.engines`` entry-point group. The returned engine
    is already started and is also an async context manager, so both of these
    work::

        engine = await open_engine("chrome")
        async with await open_engine("chrome") as engine: ...
    """
    cls = engine_registry.get(name)
    await cls.probe()
    engine: BrowserEngine = cls(**options)
    await engine.start()
    return engine


def available_engines() -> list[str]:
    """Every registered engine name, installed plugins included."""
    return engine_registry.names()
