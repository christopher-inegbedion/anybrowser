"""The link to the Safari Web Extension.

Same shape as the Chrome extension pipe, and for the same reason: an extension
can only make connections, so the engine listens and the extension dials in.

What travels over it is different, though. The Chrome bridge relays CDP; Safari
has no CDP, so this speaks a small purpose-built vocabulary -- perceive, read,
pointer, evaluate, navigate -- implemented by the content script. Input that
must be *trusted* does not come through here at all: it goes to the native
helper's accessibility layer, because a synthetic click carries no user
activation and silently fails on anything that checks for it.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
from typing import Any

from ...core.errors import EngineError, EngineNotAvailable, TransportError

logger = logging.getLogger(__name__)

__all__ = ["SafariExtensionChannel"]


class SafariExtensionChannel:
    """Listens for the Safari extension and speaks to the page through it."""

    def __init__(
        self, *, host: str = "127.0.0.1", port: int = 8788, connect_timeout: float = 60.0
    ) -> None:
        self._host = host
        self._port = port
        self._connect_timeout = connect_timeout
        self._server: Any = None
        self._socket: Any = None
        self._ids = itertools.count(1)
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._connected = asyncio.Event()
        self._hello: dict[str, Any] = {}

    @property
    def browser_description(self) -> str:
        return str(self._hello.get("browser") or "")

    @property
    def connected(self) -> bool:
        return self._socket is not None

    async def start(self) -> None:
        if self._server is not None:
            await self._await_extension()
            return
        try:
            import websockets
        except ImportError as exc:
            raise EngineNotAvailable(
                "the Safari extension channel needs the 'websockets' package; "
                "pip install 'relaykit[safari]'"
            ) from exc

        async def _on_extension(socket: Any) -> None:
            self._socket = socket
            self._connected.set()
            try:
                async for raw in socket:
                    self._route(raw)
            finally:
                if self._socket is socket:
                    self._socket = None
                    self._connected.clear()
                    # Anything in flight will never be answered now. Failing it
                    # immediately turns a dropped extension into a clear error
                    # rather than one timeout per pending call.
                    for future in self._pending.values():
                        if not future.done():
                            future.set_exception(
                                TransportError("the Safari extension disconnected")
                            )
                    self._pending.clear()

        self._server = await websockets.serve(
            _on_extension, self._host, self._port, max_size=64 * 1024 * 1024
        )
        await self._await_extension()

    async def _await_extension(self) -> None:
        try:
            await asyncio.wait_for(self._connected.wait(), self._connect_timeout)
        except asyncio.TimeoutError as exc:
            raise EngineNotAvailable(
                f"no RelayKit Safari extension connected to ws://{self._host}:{self._port} "
                f"within {self._connect_timeout:g}s. Build it with "
                "scripts/build_safari_extension.py, convert it with "
                "safari-web-extension-converter, and enable it in Safari settings.",
            ) from exc

    async def close(self) -> None:
        if self._socket is not None:
            with contextlib.suppress(Exception):
                await self._socket.close()
            self._socket = None
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        self._connected.clear()

    def _route(self, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("undecodable frame from the Safari extension")
            return
        if not isinstance(message, dict):
            return
        message_id = message.get("id")
        if message_id is not None:
            future = self._pending.pop(str(message_id), None)
            if future is not None and not future.done():
                future.set_result(message)
            return
        if message.get("type") == "hello":
            self._hello = message

    async def request(
        self, kind: str, payload: dict[str, Any] | None = None, *, timeout: float = 20.0
    ) -> dict[str, Any]:
        if self._socket is None:
            raise TransportError("no Safari extension is connected")
        message_id = str(next(self._ids))
        body = {"id": message_id, "type": kind, **(payload or {})}
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        try:
            await self._socket.send(json.dumps(body))
            reply = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(message_id, None)
            raise EngineError(
                f"the Safari extension did not answer {kind}", timeout=timeout
            ) from exc
        finally:
            self._pending.pop(message_id, None)

        if not reply.get("ok"):
            raise EngineError(str(reply.get("error") or f"{kind} failed"))
        return dict(reply.get("result") or {})
