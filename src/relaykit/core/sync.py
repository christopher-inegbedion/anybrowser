"""Blocking access to an async engine, with the event loop owned in one place.

The interface is async because every backend is. Plenty of callers are not --
scripts, notebooks, and the ported executor. ``SyncEngine`` runs one background
loop and proxies to it, so the loop-hiding that would otherwise be duplicated in
each backend happens exactly once, here.

    engine = SyncEngine(ChromeEngine(port=9222))
    engine.start()
    engine.navigate("https://example.com")
    shot = engine.screenshot()
    engine.close()

Every public coroutine on :class:`~relaykit.core.engine.BrowserEngine` is
available with the same name and signature, blocking instead of awaitable.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from concurrent.futures import Future
from typing import Any, TypeVar

from .engine import BrowserEngine, Capabilities

__all__ = ["SyncEngine"]

T = TypeVar("T")


class SyncEngine:
    """Blocking facade over a :class:`BrowserEngine`."""

    def __init__(self, engine: BrowserEngine, *, timeout: float = 120.0) -> None:
        self._engine = engine
        self._timeout = timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # -- loop ownership ------------------------------------------------ #

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is not None:
                return self._loop
            ready: Future[asyncio.AbstractEventLoop] = Future()

            def _run() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                ready.set_result(loop)
                loop.run_forever()

            self._thread = threading.Thread(target=_run, name="relaykit-engine-loop", daemon=True)
            self._thread.start()
            self._loop = ready.result(timeout=10)
            return self._loop

    def run(self, coro: Any, *, timeout: float | None = None) -> Any:
        """Run one coroutine on the engine loop and block for its result."""
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout if timeout is not None else self._timeout)

    # -- lifecycle ----------------------------------------------------- #

    @property
    def engine(self) -> BrowserEngine:
        """The wrapped async engine, for code that is already on the loop."""
        return self._engine

    @property
    def capabilities(self) -> Capabilities:
        return self._engine.capabilities

    def close(self) -> None:
        if self._loop is None:
            return
        try:
            self.run(self._engine.close())
        finally:
            loop, self._loop = self._loop, None
            loop.call_soon_threadsafe(loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=5)
                self._thread = None

    def __enter__(self) -> SyncEngine:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- proxying ------------------------------------------------------ #

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        attr = getattr(self._engine, name, None)
        if attr is None:
            raise AttributeError(f"{type(self._engine).__name__} has no attribute {name!r}")
        if not callable(attr):
            return attr
        if not inspect.iscoroutinefunction(attr):
            return attr

        def _blocking(*args: Any, **kwargs: Any) -> Any:
            return self.run(attr(*args, **kwargs))

        _blocking.__name__ = name
        _blocking.__doc__ = attr.__doc__
        return _blocking

    def __dir__(self) -> list[str]:
        return sorted({*super().__dir__(), *dir(self._engine)})
