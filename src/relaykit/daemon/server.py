"""The daemon: one browser, many clients.

A browser is a singleton and several things want it -- a CLI, a UI, a scheduled
run, an agent. The daemon owns one :class:`~relaykit.core.engine.BrowserEngine`
and serves it over a :class:`~relaykit.daemon.transport.DaemonTransport`.

    engine = await open_engine("chrome")
    server = DaemonServer(engine, UnixSocketTransport())
    await server.serve()          # until cancelled

It knows nothing about sockets and nothing about which engine it holds. Both are
injected, which is why the same server serves Chrome on a Unix socket and a
test double over an in-process queue.

Authorisation
-------------
Every method is dispatched through :class:`AuthPolicy`. The default one accepts
everything, which is correct for a Unix socket at mode 0600 -- the filesystem
already decided who may connect. Anything reachable over the network should pass
a real policy; the daemon is complete control of a browser holding live logins.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from ..core.engine import BrowserEngine
from ..core.errors import PermissionDenied, RelayKitError
from ..core.types import KeyModifier, MouseButton, Point
from . import codec
from .protocol import PROTOCOL_VERSION, Event, Message, Request, Response
from .transport import Connection, DaemonTransport

logger = logging.getLogger(__name__)

__all__ = ["AllowAll", "AuthPolicy", "DaemonServer", "TokenAuth"]

Handler = Callable[..., Awaitable[Any]]


class AuthPolicy:
    """Decides whether a connection may call a method."""

    async def authorise(self, connection: Connection, method: str) -> None:
        """Raise :class:`PermissionDenied` to refuse."""
        raise NotImplementedError


class AllowAll(AuthPolicy):
    """Accept everything.

    The right default for a Unix socket at mode 0600, where the filesystem has
    already answered the question. Not the right default for anything else.
    """

    async def authorise(self, connection: Connection, method: str) -> None:
        return None


class TokenAuth(AuthPolicy):
    """Require a shared secret, presented once per connection via ``session.hello``.

    Deliberately minimal: it is a bearer token compared in constant time, not an
    identity system. Use it when the transport cannot answer "who is this?" on
    its own -- which in practice means anything that is not a Unix socket.
    """

    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("TokenAuth needs a non-empty token")
        self._token = token
        self._authorised: set[str] = set()

    def present(self, connection: Connection, token: str) -> bool:
        import hmac

        if hmac.compare_digest(token, self._token):
            self._authorised.add(connection.id)
            return True
        return False

    async def authorise(self, connection: Connection, method: str) -> None:
        if method == "session.hello":
            return None
        if connection.id not in self._authorised:
            raise PermissionDenied("present a valid token via session.hello first")


class DaemonServer:
    """Serves one engine to many clients."""

    def __init__(
        self,
        engine: BrowserEngine,
        transport: DaemonTransport,
        *,
        auth: AuthPolicy | None = None,
    ) -> None:
        self._engine = engine
        self._transport = transport
        self._auth = auth or AllowAll()
        self._methods: dict[str, Handler] = {}
        self._register_methods()

    @property
    def address(self) -> str:
        return self._transport.address

    @property
    def engine(self) -> BrowserEngine:
        return self._engine

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def serve(self) -> None:
        """Listen until cancelled."""
        await self._transport.serve(self._handle)

    async def close(self) -> None:
        await self._transport.close()

    async def broadcast(self, event: Event) -> None:
        """Send an event to every attached client."""
        async for connection in self._transport.connections():
            await connection.send(event)

    # ------------------------------------------------------------------ #
    # Dispatch                                                            #
    # ------------------------------------------------------------------ #

    async def _handle(self, connection: Connection, message: Message) -> Message | None:
        if not isinstance(message, Request):
            # Clients do not send us responses, and an event from a client is
            # informational. Neither gets a reply.
            return None

        handler = self._methods.get(message.method)
        if handler is None:
            return Response(
                id=message.id,
                ok=False,
                error=f"unknown method: {message.method}",
                error_type="UnknownMethod",
            )

        try:
            await self._auth.authorise(connection, message.method)
            params = dict(message.params)
            if _wants_connection(handler):
                result = await handler(connection, **params)
            else:
                result = await handler(**params)
        except RelayKitError as exc:
            # A typed failure is information the client needs, not a server
            # fault: the error class is how a caller tells "retry" from
            # "re-snapshot" from "never".
            return Response(id=message.id, ok=False, error=str(exc), error_type=type(exc).__name__)
        except TypeError as exc:
            return Response(
                id=message.id, ok=False, error=f"bad parameters: {exc}", error_type="TypeError"
            )
        except Exception as exc:
            logger.exception("handler failed: %s", message.method)
            return Response(id=message.id, ok=False, error=str(exc), error_type=type(exc).__name__)
        return Response(id=message.id, result=result)

    def _register_methods(self) -> None:
        engine = self._engine
        m = self._methods

        # -- session -------------------------------------------------- #

        async def hello(connection: Connection, token: str = "", **_: Any) -> dict[str, Any]:
            if isinstance(self._auth, TokenAuth) and not self._auth.present(connection, token):
                raise PermissionDenied("invalid token")
            info = await engine.info()
            return {
                "protocol_version": PROTOCOL_VERSION,
                "engine": {
                    "name": info.name,
                    "browser": info.browser,
                    "browser_version": info.browser_version,
                    "platform": info.platform,
                    "engine_version": info.engine_version,
                },
                "capabilities": sorted(c.value for c in engine.capabilities.supported),
                "capability_notes": {
                    c.value: note for c, note in engine.capabilities.notes.items()
                },
            }

        m["session.hello"] = hello

        # -- observation ---------------------------------------------- #

        async def url() -> str:
            return await engine.url()

        async def title() -> str:
            return await engine.title()

        async def viewport() -> dict[str, Any]:
            return codec.dump_viewport(await engine.viewport())

        async def snapshot(include_text: bool = True) -> dict[str, Any]:
            return codec.dump_snapshot(await engine.snapshot(include_text=include_text))

        async def screenshot(
            full_page: bool = False, clip: Mapping[str, Any] | None = None
        ) -> dict[str, Any]:
            box = codec.load_box(clip) if clip else None
            return codec.dump_screenshot(await engine.screenshot(full_page=full_page, clip=box))

        async def frames() -> list[dict[str, Any]]:
            return [
                {
                    "frame_id": f.frame_id,
                    "url": f.url,
                    "parent_id": f.parent_id,
                    "is_main": f.is_main,
                    "cross_origin": f.cross_origin,
                }
                for f in await engine.frames()
            ]

        m.update(
            {
                "engine.url": url,
                "engine.title": title,
                "engine.viewport": viewport,
                "engine.snapshot": snapshot,
                "engine.screenshot": screenshot,
                "engine.frames": frames,
            }
        )

        # -- navigation ------------------------------------------------ #

        async def navigate(url: str, timeout: float = 30.0) -> dict[str, Any]:
            return codec.dump_navigation(await engine.navigate(url, timeout=timeout))

        async def reload(timeout: float = 30.0) -> dict[str, Any]:
            return codec.dump_navigation(await engine.reload(timeout=timeout))

        async def go_back(timeout: float = 30.0) -> dict[str, Any]:
            return codec.dump_navigation(await engine.go_back(timeout=timeout))

        async def go_forward(timeout: float = 30.0) -> dict[str, Any]:
            return codec.dump_navigation(await engine.go_forward(timeout=timeout))

        m.update(
            {
                "engine.navigate": navigate,
                "engine.reload": reload,
                "engine.go_back": go_back,
                "engine.go_forward": go_forward,
            }
        )

        # -- input ----------------------------------------------------- #

        async def click(
            target: Mapping[str, Any],
            button: str = "left",
            click_count: int = 1,
            modifiers: list[str] | None = None,
        ) -> dict[str, Any]:
            return codec.dump_outcome(
                await engine.click(
                    codec.load_point_or_element(target),
                    button=MouseButton(button),
                    click_count=click_count,
                    modifiers=[KeyModifier(k) for k in modifiers or ()],
                )
            )

        async def type_text(
            text: str,
            target: Mapping[str, Any] | None = None,
            clear_first: bool = False,
            delay: float = 0.0,
        ) -> dict[str, Any]:
            return codec.dump_outcome(
                await engine.type_text(
                    text,
                    target=codec.load_point_or_element(target) if target else None,
                    clear_first=clear_first,
                    delay=delay,
                )
            )

        async def press_key(
            key: str, modifiers: list[str] | None = None, repeat: int = 1
        ) -> dict[str, Any]:
            return codec.dump_outcome(
                await engine.press_key(
                    key, modifiers=[KeyModifier(k) for k in modifiers or ()], repeat=repeat
                )
            )

        async def scroll(
            delta_x: float, delta_y: float, at: Mapping[str, Any] | None = None
        ) -> dict[str, Any]:
            point = Point(float(at["x"]), float(at["y"])) if at else None
            return codec.dump_outcome(await engine.scroll(delta_x, delta_y, at=point))

        async def hover(target: Mapping[str, Any]) -> dict[str, Any]:
            return codec.dump_outcome(await engine.hover(codec.load_point_or_element(target)))

        async def drag(
            path: list[Mapping[str, Any]], button: str = "left", hold: float = 0.0
        ) -> dict[str, Any]:
            points = [Point(float(p["x"]), float(p["y"])) for p in path]
            return codec.dump_outcome(
                await engine.drag(points, button=MouseButton(button), hold=hold)
            )

        async def select_option(
            target: Mapping[str, Any], value: str = "", label: str = "", index: int = -1
        ) -> dict[str, Any]:
            return codec.dump_outcome(
                await engine.select_option(
                    codec.load_element(target), value=value, label=label, index=index
                )
            )

        async def upload_files(target: Mapping[str, Any], paths: list[str]) -> dict[str, Any]:
            return codec.dump_outcome(await engine.upload_files(codec.load_element(target), paths))

        async def set_zoom(factor: float) -> dict[str, Any]:
            return codec.dump_outcome(await engine.set_zoom(factor))

        m.update(
            {
                "engine.click": click,
                "engine.type_text": type_text,
                "engine.press_key": press_key,
                "engine.scroll": scroll,
                "engine.hover": hover,
                "engine.drag": drag,
                "engine.select_option": select_option,
                "engine.upload_files": upload_files,
                "engine.set_zoom": set_zoom,
            }
        )

        # -- scripting, tabs, cookies ---------------------------------- #

        async def evaluate(script: str, args: list[Any] | None = None, frame_id: str = "") -> Any:
            return await engine.evaluate(script, *(args or ()), frame_id=frame_id)

        async def add_init_script(script: str) -> None:
            await engine.add_init_script(script)

        async def tabs() -> list[dict[str, Any]]:
            return [codec.dump_tab(t) for t in await engine.tabs()]

        async def active_tab() -> dict[str, Any]:
            return codec.dump_tab(await engine.active_tab())

        async def switch_tab(tab_id: str) -> dict[str, Any]:
            return codec.dump_tab(await engine.switch_tab(tab_id))

        async def open_tab(url: str = "") -> dict[str, Any]:
            return codec.dump_tab(await engine.open_tab(url))

        async def close_tab(tab_id: str) -> None:
            await engine.close_tab(tab_id)

        async def handle_dialog(accept: bool, prompt_text: str = "") -> dict[str, Any]:
            return codec.dump_outcome(
                await engine.handle_dialog(accept=accept, prompt_text=prompt_text)
            )

        async def cookies(urls: list[str] | None = None) -> list[Mapping[str, Any]]:
            return [dict(c) for c in await engine.cookies(urls or ())]

        async def set_cookies(cookies_: list[Mapping[str, Any]]) -> None:
            await engine.set_cookies(cookies_)

        m.update(
            {
                "engine.evaluate": evaluate,
                "engine.add_init_script": add_init_script,
                "engine.tabs": tabs,
                "engine.active_tab": active_tab,
                "engine.switch_tab": switch_tab,
                "engine.open_tab": open_tab,
                "engine.close_tab": close_tab,
                "engine.handle_dialog": handle_dialog,
                "engine.cookies": cookies,
                "engine.set_cookies": set_cookies,
            }
        )


def _wants_connection(handler: Handler) -> bool:
    try:
        first = next(iter(inspect.signature(handler).parameters))
    except (StopIteration, ValueError, TypeError):
        return False
    return first == "connection"


async def serve_forever(
    engine: BrowserEngine, transport: DaemonTransport, *, auth: AuthPolicy | None = None
) -> None:
    """Convenience: build a server and run it until cancelled."""
    server = DaemonServer(engine, transport, auth=auth)
    try:
        await server.serve()
    except asyncio.CancelledError:
        raise
    finally:
        await server.close()
