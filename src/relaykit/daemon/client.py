"""``RemoteEngine`` -- a BrowserEngine that lives at the other end of a daemon.

The client of a daemon is not a new kind of object. It is an engine:

    engine = await RemoteEngine.connect("unix", "/run/relaykit.sock")
    await engine.navigate("https://example.com")
    page = await engine.snapshot()

That has a consequence worth stating plainly: **the engine conformance suite
grades the daemon.** Point it at ``--engine remote`` and all 32 tests run
through the transport, the protocol, the codec, the dispatch table and the real
engine at the far end. Nothing about the daemon needs its own contract, because
the daemon's job is to be indistinguishable from the engine it holds -- and that
is exactly what the suite already checks.

Capabilities come from the far end. A remote engine over Chrome declares what
that Chrome declares, so callers routing on capabilities behave identically
whether the browser is in this process or another one.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping, Sequence
from typing import Any

from ..core.engine import BrowserEngine, Capabilities, Capability, EngineInfo
from ..core.errors import (
    ActionFailed,
    CapabilityNotSupported,
    ElementNotFound,
    EngineError,
    EngineNotAvailable,
    EvaluationError,
    NavigationError,
    PermissionDenied,
    RelayKitError,
    StaleHandle,
    TabNotFound,
    TransportError,
)
from ..core.registry import transports
from ..core.types import (
    ActionOutcome,
    Box,
    Element,
    FrameInfo,
    KeyModifier,
    MouseButton,
    NavigationResult,
    Point,
    Screenshot,
    Snapshot,
    TabInfo,
    Viewport,
)
from . import codec
from .protocol import Event, Request, Response
from .transport import DaemonClient

__all__ = ["RemoteEngine"]

#: Rebuild the far end's exception from its name. Losing the class would cost
#: the caller the distinction the hierarchy exists for -- StaleHandle means
#: re-snapshot, CapabilityNotSupported means never, ActionFailed means retry --
#: and a daemon that flattened all three to "error" would make every client
#: guess.
_ERRORS: dict[str, type[RelayKitError]] = {
    cls.__name__: cls
    for cls in (
        ActionFailed,
        CapabilityNotSupported,
        ElementNotFound,
        EngineError,
        EngineNotAvailable,
        EvaluationError,
        NavigationError,
        PermissionDenied,
        StaleHandle,
        TabNotFound,
        TransportError,
    )
}


class RemoteEngine(BrowserEngine):
    """An engine served by a daemon."""

    name = "remote"

    def __init__(
        self,
        *,
        transport: str = "unix",
        address: str = "",
        token: str = "",
        client: DaemonClient | None = None,
        timeout: float = 60.0,
        **_options: Any,
    ) -> None:
        self._transport_name = transport
        self._address = address
        self._token = token
        self._client = client
        self._timeout = timeout
        self._capabilities = Capabilities.of()
        self._info = EngineInfo(name=self.name)
        self._pending: dict[str, asyncio.Future[Response]] = {}
        self._events: asyncio.Queue[Event] = asyncio.Queue()
        self._reader: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    @classmethod
    async def connect(
        cls, transport: str, address: str, *, token: str = "", **options: Any
    ) -> RemoteEngine:
        engine = cls(transport=transport, address=address, token=token, **options)
        await engine.start()
        return engine

    @property
    def capabilities(self) -> Capabilities:
        return self._capabilities

    async def info(self) -> EngineInfo:
        return self._info

    async def start(self) -> None:
        if self._reader is not None:
            return
        if self._client is None:
            if not self._address:
                raise TransportError("RemoteEngine needs an address")
            transport_cls = transports.get(self._transport_name)
            self._client = await transport_cls.client_class.connect(self._address)
        self._reader = asyncio.create_task(self._read_loop())
        await self._handshake()

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(BaseException):
                await self._reader
            self._reader = None
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.close()
            self._client = None

    async def _handshake(self) -> None:
        """Learn what the far end is and what it can do.

        Capabilities are adopted rather than assumed, so a caller branching on
        them gets the same answer whether the browser is local or remote.
        """
        result = await self._call("session.hello", {"token": self._token})
        engine = result.get("engine") or {}
        # `name` stays "remote": this object is a RemoteEngine, and reporting
        # the far end's name would tell a caller they hold a local Chrome when
        # they hold a socket. The browser identity is real and passes through
        # unchanged; the far end's engine name goes in `detail`, where it
        # informs without impersonating.
        self._info = EngineInfo(
            name=self.name,
            browser=str(engine.get("browser") or ""),
            browser_version=str(engine.get("browser_version") or ""),
            platform=str(engine.get("platform") or ""),
            engine_version=str(engine.get("engine_version") or ""),
            detail={
                "remote_engine": str(engine.get("name") or ""),
                "served_by": self._transport_name,
                "address": self._address,
                "protocol_version": result.get("protocol_version"),
            },
        )
        supported = set()
        for value in result.get("capabilities") or ():
            try:
                supported.add(Capability(value))
            except ValueError:
                # A newer daemon may know capabilities this client does not.
                # Ignoring them is correct: we cannot route on what we cannot
                # name, and refusing to connect over it would be worse.
                continue
        notes = {}
        for value, note in (result.get("capability_notes") or {}).items():
            try:
                notes[Capability(value)] = str(note)
            except ValueError:
                continue
        self._capabilities = Capabilities(supported=frozenset(supported), notes=notes)

    # ------------------------------------------------------------------ #
    # Transport                                                           #
    # ------------------------------------------------------------------ #

    async def _read_loop(self) -> None:
        assert self._client is not None
        while True:
            try:
                message = await self._client.receive(timeout=3600)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._fail_pending(TransportError("daemon connection closed"))
                return
            if isinstance(message, Response):
                future = self._pending.pop(message.id, None)
                if future is not None and not future.done():
                    future.set_result(message)
            elif isinstance(message, Event):
                await self._events.put(message)

    def _fail_pending(self, exc: BaseException) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    async def _call(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        if self._client is None:
            raise TransportError("not connected")
        request = Request(method=method, params=params or {})
        future: asyncio.Future[Response] = asyncio.get_running_loop().create_future()
        self._pending[request.id] = future
        try:
            await self._client.send(request)
            response = await asyncio.wait_for(future, self._timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(request.id, None)
            raise EngineError(f"daemon did not answer {method}", timeout=self._timeout) from exc
        finally:
            self._pending.pop(request.id, None)

        if not response.ok:
            error_cls = _ERRORS.get(response.error_type, EngineError)
            raise error_cls(response.error or f"{method} failed")
        return response.result

    async def next_event(self, *, timeout: float = 30.0) -> Event:
        """Wait for the next event the daemon pushed."""
        return await asyncio.wait_for(self._events.get(), timeout)

    # ------------------------------------------------------------------ #
    # Observation                                                         #
    # ------------------------------------------------------------------ #

    async def url(self) -> str:
        return str(await self._call("engine.url"))

    async def title(self) -> str:
        return str(await self._call("engine.title"))

    async def viewport(self) -> Viewport:
        return codec.load_viewport(await self._call("engine.viewport"))

    async def snapshot(self, *, include_text: bool = True) -> Snapshot:
        return codec.load_snapshot(
            await self._call("engine.snapshot", {"include_text": include_text})
        )

    async def screenshot(self, *, full_page: bool = False, clip: Box | None = None) -> Screenshot:
        return codec.load_screenshot(
            await self._call(
                "engine.screenshot",
                {"full_page": full_page, "clip": codec.dump_box(clip) if clip else None},
            )
        )

    async def frames(self) -> Sequence[FrameInfo]:
        return tuple(
            FrameInfo(
                frame_id=str(f.get("frame_id", "")),
                url=str(f.get("url", "")),
                parent_id=str(f.get("parent_id", "")),
                is_main=bool(f.get("is_main")),
                cross_origin=bool(f.get("cross_origin")),
            )
            for f in await self._call("engine.frames")
        )

    # ------------------------------------------------------------------ #
    # Navigation                                                          #
    # ------------------------------------------------------------------ #

    async def navigate(self, url: str, *, timeout: float = 30.0) -> NavigationResult:
        return codec.load_navigation(
            await self._call("engine.navigate", {"url": url, "timeout": timeout})
        )

    async def reload(self, *, timeout: float = 30.0) -> NavigationResult:
        return codec.load_navigation(await self._call("engine.reload", {"timeout": timeout}))

    async def go_back(self, *, timeout: float = 30.0) -> NavigationResult:
        return codec.load_navigation(await self._call("engine.go_back", {"timeout": timeout}))

    async def go_forward(self, *, timeout: float = 30.0) -> NavigationResult:
        return codec.load_navigation(await self._call("engine.go_forward", {"timeout": timeout}))

    # ------------------------------------------------------------------ #
    # Input                                                               #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _target(target: Element | Point) -> dict[str, Any]:
        if isinstance(target, Element):
            return codec.dump_element(target)
        return {"x": target.x, "y": target.y}

    async def click(
        self,
        target: Element | Point,
        *,
        button: MouseButton = MouseButton.LEFT,
        click_count: int = 1,
        modifiers: Sequence[KeyModifier] = (),
    ) -> ActionOutcome:
        return codec.load_outcome(
            await self._call(
                "engine.click",
                {
                    "target": self._target(target),
                    "button": button.value,
                    "click_count": click_count,
                    "modifiers": [m.value for m in modifiers],
                },
            )
        )

    async def type_text(
        self,
        text: str,
        *,
        target: Element | Point | None = None,
        clear_first: bool = False,
        delay: float = 0.0,
    ) -> ActionOutcome:
        return codec.load_outcome(
            await self._call(
                "engine.type_text",
                {
                    "text": text,
                    "target": self._target(target) if target is not None else None,
                    "clear_first": clear_first,
                    "delay": delay,
                },
            )
        )

    async def press_key(
        self, key: str, *, modifiers: Sequence[KeyModifier] = (), repeat: int = 1
    ) -> ActionOutcome:
        return codec.load_outcome(
            await self._call(
                "engine.press_key",
                {"key": key, "modifiers": [m.value for m in modifiers], "repeat": repeat},
            )
        )

    async def scroll(
        self, delta_x: float, delta_y: float, *, at: Point | None = None
    ) -> ActionOutcome:
        return codec.load_outcome(
            await self._call(
                "engine.scroll",
                {
                    "delta_x": delta_x,
                    "delta_y": delta_y,
                    "at": {"x": at.x, "y": at.y} if at else None,
                },
            )
        )

    async def hover(self, target: Element | Point) -> ActionOutcome:
        return codec.load_outcome(
            await self._call("engine.hover", {"target": self._target(target)})
        )

    async def drag(
        self, path: Sequence[Point], *, button: MouseButton = MouseButton.LEFT, hold: float = 0.0
    ) -> ActionOutcome:
        return codec.load_outcome(
            await self._call(
                "engine.drag",
                {
                    "path": [{"x": p.x, "y": p.y} for p in path],
                    "button": button.value,
                    "hold": hold,
                },
            )
        )

    async def select_option(
        self, target: Element, *, value: str = "", label: str = "", index: int = -1
    ) -> ActionOutcome:
        return codec.load_outcome(
            await self._call(
                "engine.select_option",
                {
                    "target": codec.dump_element(target),
                    "value": value,
                    "label": label,
                    "index": index,
                },
            )
        )

    async def upload_files(self, target: Element, paths: Sequence[str]) -> ActionOutcome:
        return codec.load_outcome(
            await self._call(
                "engine.upload_files",
                {"target": codec.dump_element(target), "paths": list(paths)},
            )
        )

    async def set_zoom(self, factor: float) -> ActionOutcome:
        return codec.load_outcome(await self._call("engine.set_zoom", {"factor": factor}))

    # ------------------------------------------------------------------ #
    # Scripting, tabs, cookies                                            #
    # ------------------------------------------------------------------ #

    async def evaluate(self, script: str, *args: Any, frame_id: str = "") -> Any:
        return await self._call(
            "engine.evaluate", {"script": script, "args": list(args), "frame_id": frame_id}
        )

    async def add_init_script(self, script: str) -> None:
        await self._call("engine.add_init_script", {"script": script})

    async def tabs(self) -> Sequence[TabInfo]:
        return tuple(codec.load_tab(t) for t in await self._call("engine.tabs"))

    async def active_tab(self) -> TabInfo:
        return codec.load_tab(await self._call("engine.active_tab"))

    async def switch_tab(self, tab_id: str) -> TabInfo:
        return codec.load_tab(await self._call("engine.switch_tab", {"tab_id": tab_id}))

    async def open_tab(self, url: str = "") -> TabInfo:
        return codec.load_tab(await self._call("engine.open_tab", {"url": url}))

    async def close_tab(self, tab_id: str) -> None:
        await self._call("engine.close_tab", {"tab_id": tab_id})

    async def handle_dialog(self, *, accept: bool, prompt_text: str = "") -> ActionOutcome:
        return codec.load_outcome(
            await self._call("engine.handle_dialog", {"accept": accept, "prompt_text": prompt_text})
        )

    async def cookies(self, urls: Sequence[str] = ()) -> Sequence[Mapping[str, Any]]:
        return list(await self._call("engine.cookies", {"urls": list(urls)}))

    async def set_cookies(self, cookies: Sequence[Mapping[str, Any]]) -> None:
        await self._call("engine.set_cookies", {"cookies_": [dict(c) for c in cookies]})
