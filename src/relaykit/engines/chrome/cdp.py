"""How CDP commands reach Chrome.

The Chrome engine has two ways to talk to a browser, and they differ only in the
pipe:

``devtools``
    A direct WebSocket to a browser started with ``--remote-debugging-port``.
    Standard, dependency-light, and the only mode that can run in CI. It cannot
    attach to a browser the user started normally -- the flag has to be there
    from launch.

``extension``
    CDP relayed through a browser extension holding a ``chrome.debugger``
    session. This is the mode that matters in production: it attaches to the
    browser the user already has open, with their tabs and their logins, which
    no flag-based approach can do.

Everything above this file -- the whole engine -- is written against
:class:`CdpConnection` and does not know which one it has. That is what lets the
conformance suite grade the production engine using the CI-able pipe.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import itertools
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ...core.errors import EngineError, EngineNotAvailable, TransportError

logger = logging.getLogger(__name__)

__all__ = ["CdpConnection", "CdpEventHandler", "DevToolsConnection"]

#: Called with (method, params) for every CDP event on any session.
CdpEventHandler = Callable[[str, dict[str, Any]], Awaitable[None] | None]


class CdpConnection(abc.ABC):
    """A duplex CDP channel."""

    @abc.abstractmethod
    async def connect(self) -> None: ...

    @abc.abstractmethod
    async def close(self) -> None: ...

    @abc.abstractmethod
    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str = "",
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Issue one CDP command and return its result.

        ``session_id`` routes into a specific target. Getting this wrong is the
        classic out-of-process-iframe bug: a command sent on the page session
        silently executes in the wrong context, so a write into a cross-origin
        form is never verified and the agent loops looking for what it just
        typed.
        """

    @abc.abstractmethod
    def on_event(self, handler: CdpEventHandler) -> None:
        """Register an event sink. Replaces any previous one."""

    @property
    @abc.abstractmethod
    def attaches_to_user_session(self) -> bool:
        """Whether this pipe reaches a browser the user started themselves.

        The engine's declared ``ATTACH_TO_USER_SESSION`` capability comes from
        here, so the two can never disagree.
        """


class DevToolsConnection(CdpConnection):
    """CDP over the DevTools WebSocket.

    Multiplexes every session over one socket, which is how CDP itself works:
    commands carry a monotonically increasing ``id`` and an optional
    ``sessionId``, and replies come back out of order.
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 9222,
        websocket_url: str = "",
    ) -> None:
        self._host = host
        self._port = port
        self._url = websocket_url
        self._socket: Any = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._handler: CdpEventHandler | None = None
        self._reader: asyncio.Task[None] | None = None

    @property
    def attaches_to_user_session(self) -> bool:
        # --remote-debugging-port has to be present from launch, so this pipe
        # only ever reaches a browser started for automation.
        return False

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def _discover_url(self) -> str:
        """Ask the browser for its own debugger endpoint."""
        import urllib.error
        import urllib.request

        endpoint = f"http://{self._host}:{self._port}/json/version"
        try:
            with urllib.request.urlopen(endpoint, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            raise EngineNotAvailable(
                f"no Chrome DevTools endpoint at {self._host}:{self._port}; "
                "start Chrome with --remote-debugging-port",
                endpoint=endpoint,
            ) from exc
        url = payload.get("webSocketDebuggerUrl", "")
        if not url:
            raise EngineNotAvailable("DevTools endpoint reported no webSocketDebuggerUrl")
        return str(url)

    async def connect(self) -> None:
        if self._socket is not None:
            return
        try:
            import websockets
        except ImportError as exc:
            raise EngineNotAvailable(
                "the Chrome engine needs the 'websockets' package; pip install 'relaykit[chrome]'"
            ) from exc

        url = self._url or await self._discover_url()
        # CDP messages carrying a full-page screenshot routinely exceed the
        # default 1MiB frame cap, and the failure looks like a dropped
        # connection rather than an oversized message.
        self._socket = await websockets.connect(url, max_size=100 * 1024 * 1024)
        self._reader = asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(BaseException):
                await self._reader
            self._reader = None
        if self._socket is not None:
            with contextlib.suppress(Exception):
                await self._socket.close()
            self._socket = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(TransportError("CDP connection closed"))
        self._pending.clear()

    # ------------------------------------------------------------------ #
    # Traffic                                                             #
    # ------------------------------------------------------------------ #

    def on_event(self, handler: CdpEventHandler) -> None:
        self._handler = handler

    async def _read_loop(self) -> None:
        assert self._socket is not None
        try:
            async for raw in self._socket:
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("undecodable CDP frame")
                    continue
                await self._route(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("CDP read loop ended", exc_info=True)

    async def _route(self, message: dict[str, Any]) -> None:
        message_id = message.get("id")
        if message_id is not None:
            future = self._pending.pop(int(message_id), None)
            if future is not None and not future.done():
                future.set_result(message)
            return
        method = str(message.get("method") or "")
        if method and self._handler is not None:
            result = self._handler(method, message.get("params") or {})
            if asyncio.iscoroutine(result):
                await result

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str = "",
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        if self._socket is None:
            raise TransportError("CDP connection is not open")
        message_id = next(self._ids)
        payload: dict[str, Any] = {"id": message_id, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id

        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        try:
            await self._socket.send(json.dumps(payload))
            response = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(message_id, None)
            raise EngineError(f"CDP command timed out: {method}", timeout=timeout) from exc
        finally:
            self._pending.pop(message_id, None)

        if "error" in response:
            error = response["error"] or {}
            raise EngineError(
                f"CDP command failed: {method}: {error.get('message', 'unknown')}",
                code=error.get("code"),
                method=method,
            )
        return response.get("result") or {}


#: Marks a session id the engine invented to mean "attached to this tab", as
#: opposed to one CDP actually issued.
SYNTHETIC_SESSION_PREFIX = "tab:"


class ExtensionConnection(CdpConnection):
    """CDP relayed through a browser extension.

    This is the pipe that matters. ``--remote-debugging-port`` has to be present
    from launch, so the DevTools connection can only ever reach a browser
    started for automation. ``chrome.debugger`` is handed out by the browser
    itself, so an extension can drive the window the user already has open --
    their tabs, their sessions, their logins.

    The direction is inverted from what you might expect: **the engine listens
    and the extension dials in.** A browser extension cannot accept connections,
    only make them, and having the engine host the socket also means it does not
    depend on the daemon. ``ChromeEngine(mode="extension")`` is self-contained.

    ``start()`` waits for the extension to connect, which is normal rather than
    exceptional: the browser may not be running yet.
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8787,
        connect_timeout: float = 60.0,
    ) -> None:
        self._host = host
        self._port = port
        self._connect_timeout = connect_timeout
        self._server: Any = None
        self._socket: Any = None
        self._ids = itertools.count(1)
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._handler: CdpEventHandler | None = None
        self._connected = asyncio.Event()
        self._hello: dict[str, Any] = {}
        self._pump: asyncio.Task[None] | None = None

    @property
    def attaches_to_user_session(self) -> bool:
        return True

    @property
    def browser_description(self) -> str:
        """What the extension said it is running in, for ``EngineInfo``."""
        return str(self._hello.get("browser") or "")

    async def connect(self) -> None:
        if self._server is not None:
            await self._await_extension()
            return
        try:
            import websockets
        except ImportError as exc:
            raise EngineNotAvailable(
                "the Chrome extension pipe needs the 'websockets' package; "
                "pip install 'relaykit[chrome]'"
            ) from exc

        async def _on_extension(socket: Any) -> None:
            # One bridge at a time. A second browser connecting would silently
            # take over the session, so the newest wins and the old one is
            # dropped rather than both half-working.
            self._socket = socket
            self._connected.set()
            try:
                async for raw in socket:
                    await self._route(raw)
            finally:
                if self._socket is socket:
                    self._socket = None
                    self._connected.clear()
                    # Chrome terminates a Manifest V3 service worker whenever it
                    # likes, which takes the socket with it. Anything in flight
                    # is never going to be answered, so fail it now: otherwise
                    # every pending call waits out its full timeout for a reply
                    # that cannot arrive, and a dropped worker looks like a hung
                    # browser.
                    for future in self._pending.values():
                        if not future.done():
                            future.set_exception(
                                TransportError(
                                    "the browser extension disconnected mid-request "
                                    "(its service worker was probably terminated); "
                                    "it reconnects on its own -- retry"
                                )
                            )
                    self._pending.clear()

        self._server = await websockets.serve(
            _on_extension, self._host, self._port, max_size=100 * 1024 * 1024
        )
        await self._await_extension()

    async def _await_extension(self) -> None:
        try:
            await asyncio.wait_for(self._connected.wait(), self._connect_timeout)
        except asyncio.TimeoutError as exc:
            raise EngineNotAvailable(
                f"no RelayKit browser extension connected to ws://{self._host}:{self._port} "
                f"within {self._connect_timeout:g}s. Load extensions/chrome in Chrome "
                "and check its endpoint setting.",
            ) from exc

    async def close(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
            with contextlib.suppress(BaseException):
                await self._pump
            self._pump = None
        if self._socket is not None:
            with contextlib.suppress(Exception):
                await self._socket.close()
            self._socket = None
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(TransportError("extension disconnected"))
        self._pending.clear()
        self._connected.clear()

    def on_event(self, handler: CdpEventHandler) -> None:
        self._handler = handler

    async def _route(self, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("undecodable frame from the extension")
            return
        if not isinstance(message, dict):
            return

        message_id = message.get("id")
        if message_id is not None:
            future = self._pending.pop(str(message_id), None)
            if future is not None and not future.done():
                future.set_result(message)
            return

        kind = str(message.get("type") or "")
        if kind == "hello":
            self._hello = message
            return
        if kind == "event" and self._handler is not None:
            result = self._handler(str(message.get("method") or ""), message.get("params") or {})
            if asyncio.iscoroutine(result):
                await result

    async def request(
        self, kind: str, payload: dict[str, Any] | None = None, *, timeout: float = 30.0
    ) -> dict[str, Any]:
        """Send a non-CDP request to the bridge (tabs, attach, activate)."""
        if self._socket is None:
            raise TransportError("no extension is connected")
        message_id = str(next(self._ids))
        body = {"id": message_id, "type": kind, **(payload or {})}
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        try:
            await self._socket.send(json.dumps(body))
            reply = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(message_id, None)
            # Name the CDP method, not just "cdp". A relay that stalls does so
            # on one specific command, and "the extension did not answer cdp"
            # is not enough to find out which.
            described = kind if kind != "cdp" else f"cdp {body.get('method', '?')}"
            raise EngineError(f"the extension did not answer {described}", timeout=timeout) from exc
        finally:
            self._pending.pop(message_id, None)

        if not reply.get("ok"):
            raise EngineError(str(reply.get("error") or f"{kind} failed"))
        return dict(reply.get("result") or {})

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str = "",
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        if self.tab_id is None:
            raise EngineError("the extension pipe needs a tab; call set_tab() first")
        # The engine records a synthetic "tab:<id>" as its session marker so that
        # every started/not-started check reads the same on both pipes. It is not
        # a CDP session and must never reach chrome.debugger, which answers
        # "Session with given id not found". Real ids -- from an OOPIF
        # Target.attachToTarget inside this tab -- pass through untouched.
        routed = "" if session_id.startswith(SYNTHETIC_SESSION_PREFIX) else session_id
        return await self.request(
            "cdp",
            {
                "tabId": self.tab_id,
                "method": method,
                "params": params or {},
                "sessionId": routed,
            },
            timeout=timeout,
        )

    #: Which tab CDP commands address. The DevTools pipe carries this in its
    #: session id; the extension needs it per message, because chrome.debugger
    #: is addressed by tab rather than by session.
    tab_id: int | None = None

    def set_tab(self, tab_id: int) -> None:
        self.tab_id = int(tab_id)
