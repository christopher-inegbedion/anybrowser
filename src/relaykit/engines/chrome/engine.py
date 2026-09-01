"""Chrome browser engine implemented directly on the Chrome DevTools Protocol.

The engine deliberately depends on the narrow :class:`CdpConnection` pipe.
That keeps page behaviour identical when the production extension transport is
added: only ownership of the debugger session changes, not browser semantics.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import platform
import re
import shutil
import tempfile
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ...core.engine import BrowserEngine, Capabilities, Capability, EngineInfo
from ...core.errors import (
    ActionFailed,
    CapabilityNotSupported,
    ElementNotFound,
    EngineError,
    EngineNotAvailable,
    EvaluationError,
    NavigationError,
    StaleHandle,
    TabNotFound,
)
from ...core.types import (
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
from ...perception.dom import (
    COLLECT_SCRIPT,
    READ_SCRIPT,
    build_snapshot,
    decode_handle,
)
from .cdp import (
    SYNTHETIC_SESSION_PREFIX,
    CdpConnection,
    DevToolsConnection,
    ExtensionConnection,
)

__all__ = ["PLANNED_CAPABILITIES", "ChromeEngine"]

#: Ceiling for RelayKit's own page reads. Far below the CDP default, because
#: these are milliseconds of work and a long wait means the reply is lost, not
#: slow. See ChromeEngine._dom.
_INTERNAL_READ_TIMEOUT = 8.0

logger = logging.getLogger(__name__)

#: Everything Chrome can do when reached through the extension pipe. The
#: DevTools pipe drops ATTACH_TO_USER_SESSION, because the launch flag means it
#: only ever reaches a browser started for automation -- see `capabilities`,
#: which derives that from the live connection rather than restating it.
#:
#: Defined here rather than in __init__ so the engine does not import its own
#: package: that is a circular import, and it fails at plugin-load time rather
#: than at development time.
PLANNED_CAPABILITIES = Capabilities.of(
    Capability.ATTACH_TO_USER_SESSION,
    Capability.TRUSTED_INPUT,
    Capability.BACKGROUND_INPUT,
    Capability.EVALUATE_JS,
    Capability.CROSS_ORIGIN_FRAMES,
    Capability.OFFSCREEN_SCREENSHOT,
    Capability.FULL_PAGE_SCREENSHOT,
    Capability.SCREENCAST,
    Capability.POINTER_GESTURES,
    Capability.FILE_UPLOAD,
    Capability.JS_DIALOGS,
    Capability.COOKIES,
    Capability.NETWORK_INTERCEPTION,
    Capability.TAB_MANAGEMENT,
    Capability.PAGE_ZOOM,
    Capability.INIT_SCRIPTS,
)

_CHROME_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/opt/google/chrome/chrome",
)

#: name -> (key, code, windowsVirtualKeyCode, text)
#:
#: The fourth field is what makes these work, and its absence is a trap. Enter,
#: Tab and Backspace have a *character* representation ("\r", "\t", "\x08"),
#: and Chrome expects a text-bearing key to arrive as a "keyDown" carrying it.
#: Dispatch one as a bare "rawKeyDown" instead and Chrome accepts the command,
#: answers it, and then stalls its input queue waiting for the char event that
#: never comes -- so the NEXT input command hangs until it times out. The
#: failure surfaces one action after its cause, which is what makes it
#: expensive to find. Keys with no character (Escape, the arrows) correctly
#: carry no text and are sent as rawKeyDown.
_KEYS: dict[str, tuple[str, str, int, str]] = {
    "Enter": ("Enter", "Enter", 13, "\r"),
    "Tab": ("Tab", "Tab", 9, "\t"),
    "Backspace": ("Backspace", "Backspace", 8, "\x08"),
    "Escape": ("Escape", "Escape", 27, ""),
    "Delete": ("Delete", "Delete", 46, ""),
    "ArrowLeft": ("ArrowLeft", "ArrowLeft", 37, ""),
    "ArrowUp": ("ArrowUp", "ArrowUp", 38, ""),
    "ArrowRight": ("ArrowRight", "ArrowRight", 39, ""),
    "ArrowDown": ("ArrowDown", "ArrowDown", 40, ""),
}


class ChromeEngine(BrowserEngine):
    """Drive one Chrome page through a flattened CDP target session."""

    name = "chrome"

    def __init__(
        self,
        *,
        mode: str = "devtools",
        host: str = "127.0.0.1",
        port: int = 9222,
        headless: bool = True,
        launch: bool = True,
        chrome_path: str = "",
        user_data_dir: str = "",
        extension_port: int = 8787,
        connect_timeout: float = 60.0,
        connection: CdpConnection | None = None,
    ) -> None:
        if mode not in {"devtools", "extension"}:
            raise ValueError("mode must be 'devtools' or 'extension'")
        self._mode = mode
        self._host = host
        self._port = port
        #: Where the extension dials in. The engine listens, because a browser
        #: extension can only make connections, never accept them.
        self._extension_port = extension_port
        self._connect_timeout = connect_timeout
        self._headless = headless
        self._launch = launch
        self._chrome_path = chrome_path
        self._configured_profile = user_data_dir
        self._connection = connection
        self._injected_connection = connection is not None
        self._process: asyncio.subprocess.Process | None = None
        self._temp_profile = ""
        self._session_id = ""
        self._target_id = ""
        self._generation = 0
        self._event_waiters: list[tuple[frozenset[str], asyncio.Future[str]]] = []

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    @classmethod
    async def probe(cls) -> None:
        """Confirm that a normal Chrome executable is available without launching it."""
        if cls._find_chrome("") is None:
            raise EngineNotAvailable(
                "Chrome was not found; set RELAYKIT_CHROME to the browser executable"
            )

    @property
    def capabilities(self) -> Capabilities:
        connection = self._connection
        attaches = self._mode == "extension"
        if connection is not None:
            attaches = connection.attaches_to_user_session
        if attaches:
            return PLANNED_CAPABILITIES
        supported = PLANNED_CAPABILITIES.supported - {Capability.ATTACH_TO_USER_SESSION}
        return Capabilities(supported=frozenset(supported), notes=PLANNED_CAPABILITIES.notes)

    async def info(self) -> EngineInfo:
        version = ""
        product = "Chrome"
        connection = self._connection
        if isinstance(connection, ExtensionConnection):
            # Browser.getVersion is one of the domains chrome.debugger refuses,
            # so the extension reports the user agent in its hello instead.
            agent = connection.browser_description
            match = re.search(r"Chrome/(\S+)", agent)
            if match:
                version = match.group(1)
        elif connection is not None:
            with contextlib.suppress(Exception):
                data = await connection.send("Browser.getVersion")
                product_version = str(data.get("product") or "")
                if product_version:
                    product, _, version = product_version.partition("/")
        return EngineInfo(
            name=self.name,
            browser=product or "Chrome",
            browser_version=version,
            platform=platform.platform(),
            engine_version="0.1.0",
            detail={"mode": self._mode},
        )

    async def start(self) -> None:
        if self._session_id:
            return
        if self._connection is None:
            if self._mode == "extension":
                self._connection = ExtensionConnection(
                    host=self._host,
                    port=self._extension_port,
                    connect_timeout=self._connect_timeout,
                )
            else:
                self._connection = DevToolsConnection(host=self._host, port=self._port)
        self._connection.on_event(self._on_event)
        try:
            try:
                await self._connection.connect()
            except EngineNotAvailable:
                if self._injected_connection or not self._launch:
                    raise
                await self._launch_chrome()
                await self._connection.connect()
            if isinstance(self._connection, ExtensionConnection):
                await self._attach_via_extension()
            else:
                await self._attach_page()
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        """Release all owned resources; teardown must not mask an earlier failure."""
        connection = self._connection
        session_id = self._session_id
        self._session_id = ""
        self._target_id = ""
        if connection is not None and session_id:
            with contextlib.suppress(Exception):
                await connection.send("Target.detachFromTarget", {"sessionId": session_id})
        if connection is not None:
            with contextlib.suppress(Exception):
                await connection.close()
        if not self._injected_connection:
            self._connection = None

        process = self._process
        self._process = None
        if process is not None and process.returncode is None:
            with contextlib.suppress(Exception):
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=5)
            if process.returncode is None:
                with contextlib.suppress(Exception):
                    process.kill()
                    await process.wait()
        if self._temp_profile:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(shutil.rmtree, self._temp_profile)
            self._temp_profile = ""

        for _, future in self._event_waiters:
            if not future.done():
                future.cancel()
        self._event_waiters.clear()

    @staticmethod
    def _find_chrome(override: str) -> str | None:
        candidates = [override, os.environ.get("RELAYKIT_CHROME", ""), *_CHROME_PATHS]
        for candidate in candidates:
            if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
                return candidate
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            found = shutil.which(name)
            if found:
                return found
        return None

    async def _endpoint_ready(self) -> bool:
        endpoint = f"http://{self._host}:{self._port}/json/version"

        def fetch() -> bool:
            try:
                with urllib.request.urlopen(endpoint, timeout=0.5) as response:
                    json.loads(response.read().decode("utf-8"))
                return True
            except (urllib.error.URLError, OSError, json.JSONDecodeError):
                return False

        return await asyncio.to_thread(fetch)

    async def _launch_chrome(self) -> None:
        binary = self._find_chrome(self._chrome_path)
        if binary is None:
            raise EngineNotAvailable(
                "Chrome was not found; pass chrome_path or set RELAYKIT_CHROME"
            )
        profile = self._configured_profile
        if not profile:
            profile = tempfile.mkdtemp(prefix="relaykit-chrome-")
            self._temp_profile = profile
        args = [
            binary,
            f"--remote-debugging-port={self._port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if self._headless:
            args.append("--headless=new")
        self._process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        deadline = asyncio.get_running_loop().time() + 20
        while asyncio.get_running_loop().time() < deadline:
            if self._process.returncode is not None:
                break
            if await self._endpoint_ready():
                return
            await asyncio.sleep(0.1)
        raise EngineNotAvailable(
            f"Chrome did not expose DevTools at {self._host}:{self._port} within 20 seconds"
        )

    async def _attach_page(self, target_id: str = "") -> None:
        connection = self._require_connection()
        if not target_id:
            targets = await connection.send("Target.getTargets")
            pages = [item for item in targets.get("targetInfos", ()) if item.get("type") == "page"]
            if pages:
                target_id = str(pages[0]["targetId"])
            else:
                created = await connection.send("Target.createTarget", {"url": "about:blank"})
                target_id = str(created["targetId"])
        attached = await connection.send(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}
        )
        self._target_id = target_id
        self._session_id = str(attached["sessionId"])
        for domain in ("Page", "Runtime", "DOM", "Network"):
            await self._send(f"{domain}.enable")

    async def _attach_via_extension(self) -> None:
        """Adopt a tab in the user's own browser.

        There is no target/session dance here: ``chrome.debugger`` is addressed
        by *tab*, and the extension attaches on first use. So picking a tab is
        the whole of it -- the active one, because that is the window the person
        is actually looking at.
        """
        connection = self._connection
        assert isinstance(connection, ExtensionConnection)
        listing = await connection.request("tabs")
        tabs = [t for t in listing.get("tabs", ()) if t.get("tab_id")]
        if not tabs:
            raise EngineNotAvailable("the browser reported no tabs")
        chosen = next((t for t in tabs if t.get("active")), tabs[0])
        connection.set_tab(int(chosen["tab_id"]))
        self._target_id = str(chosen["tab_id"])
        # A session id is the DevTools pipe's way of naming a tab; the extension
        # names it per message. Recording a synthetic one keeps every
        # started/not-started check identical across both pipes -- the
        # connection strips it before anything reaches chrome.debugger.
        self._session_id = f"{SYNTHETIC_SESSION_PREFIX}{chosen['tab_id']}"
        for domain in ("Page", "Runtime", "DOM"):
            with contextlib.suppress(Exception):
                await self._send(f"{domain}.enable")

    def _require_connection(self) -> CdpConnection:
        if self._connection is None:
            raise ActionFailed("engine is not started")
        return self._connection

    def _require_session(self) -> str:
        if not self._session_id:
            raise ActionFailed("engine is not started")
        return self._session_id

    async def _send(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0
    ) -> dict[str, Any]:
        return await self._require_connection().send(
            method,
            params,
            session_id=self._require_session(),
            timeout=timeout,
        )

    async def _on_event(self, method: str, _params: dict[str, Any]) -> None:
        if method == "Page.navigatedWithinDocument" or (
            method == "Page.frameNavigated" and not (_params.get("frame") or {}).get("parentId")
        ):
            # Clicks and key presses can navigate without passing through a
            # navigation method. Their handles must still become stale.
            self._generation += 1
        for methods, future in list(self._event_waiters):
            if method in methods and not future.done():
                future.set_result(method)

    def _event_future(self, *methods: str) -> asyncio.Future[str]:
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._event_waiters.append((frozenset(methods), future))
        future.add_done_callback(
            lambda done: self._event_waiters.__setitem__(
                slice(None), [item for item in self._event_waiters if item[1] is not done]
            )
        )
        return future

    # ------------------------------------------------------------------ #
    # Evaluation and observation                                         #
    # ------------------------------------------------------------------ #

    async def _runtime_evaluate(
        self,
        expression: str,
        *,
        return_by_value: bool = True,
        await_promise: bool = True,
        timeout: float = 30.0,
    ) -> Any:
        try:
            response = await self._send(
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "awaitPromise": await_promise,
                    "returnByValue": return_by_value,
                },
                timeout=timeout,
            )
        except EngineError as exc:
            raise EvaluationError(str(exc)) from exc
        if response.get("exceptionDetails"):
            details = response["exceptionDetails"]
            raise EvaluationError(
                str(details.get("text") or "JavaScript evaluation failed"),
                details=details,
            )
        remote = response.get("result") or {}
        if return_by_value:
            if "value" in remote:
                return remote["value"]
            if "unserializableValue" in remote:
                return remote["unserializableValue"]
            return None
        return remote

    async def _dom(self, script: str, argument: Mapping[str, Any]) -> Any:
        """Run one of RelayKit's own page scripts.

        Bounded and retried once, which the public :meth:`evaluate` is not.
        Driving through an extension means a service worker relays every
        command, and it occasionally loses a reply -- most often when a
        navigation tears the execution context down underneath an in-flight
        ``Runtime.evaluate``. Waiting out the full timeout for a reply that is
        never coming turns a 30ms read into a 30s stall.


        Retrying is safe *here* and only here: these scripts are RelayKit's own
        and are idempotent reads. Retrying arbitrary caller script would run
        their side effects twice, so :meth:`evaluate` keeps the full timeout and
        a single attempt.
        """
        expression = f"({script})({json.dumps(dict(argument), separators=(',', ':'))})"
        return await self._internal_evaluate(expression)

    async def _internal_evaluate(self, expression: str) -> Any:
        """Evaluate one of RelayKit's own expressions: bounded, and retried once.

        Every internal read goes through here -- url, title, the page scripts,
        the change signature -- so the policy lives in one place.

        Driving through an extension means a service worker relays each command,
        and it occasionally loses a reply; most often when a navigation tears the
        execution context down underneath an in-flight ``Runtime.evaluate``.
        Waiting out the default CDP timeout turns a 30ms read into a 30s stall.

        Retrying is safe *here* and only here: these expressions are RelayKit's
        own and are idempotent. Retrying arbitrary caller script would run their
        side effects twice, so the public :meth:`evaluate` keeps the full
        timeout and a single attempt.
        """
        try:
            return await self._runtime_evaluate(expression, timeout=_INTERNAL_READ_TIMEOUT)
        except EvaluationError as exc:
            if "did not answer" not in str(exc):
                raise
            logger.debug("page read lost its reply; retrying once")
            return await self._runtime_evaluate(expression, timeout=_INTERNAL_READ_TIMEOUT)

    async def _read(self, operation: str, **options: Any) -> Any:
        return await self._dom(READ_SCRIPT, {"op": operation, **options})

    async def url(self) -> str:
        # `chrome.debugger` refuses browser-level domains outright -- Target.*
        # and Browser.* come back as "Not allowed" -- so Target.getTargetInfo
        # works on the DevTools pipe and cannot work on the extension one.
        # location.href works on both, and is what the page would report anyway.
        value = await self._internal_evaluate("document.location.href")
        return str(value or "")

    async def title(self) -> str:
        value = await self._internal_evaluate("document.title")
        return str(value or "")

    async def viewport(self) -> Viewport:
        data = await self._read("viewport")
        return Viewport(
            width=int(data["width"]),
            height=int(data["height"]),
            scroll_x=float(data["scrollX"]),
            scroll_y=float(data["scrollY"]),
            device_pixel_ratio=float(data["dpr"]),
        )

    async def snapshot(self, *, include_text: bool = True) -> Snapshot:
        raw = await self._dom(COLLECT_SCRIPT, {"includeText": include_text})
        return build_snapshot(
            raw,
            url=await self.url(),
            title=await self.title(),
            generation=self._generation,
            frames=await self.frames(),
        )

    async def screenshot(self, *, full_page: bool = False, clip: Box | None = None) -> Screenshot:
        view = await self.viewport()
        width = view.width
        height = view.height
        params: dict[str, Any] = {"format": "png", "fromSurface": True}
        if full_page:
            metrics = await self._send("Page.getLayoutMetrics")
            content = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
            width = max(1, round(float(content.get("width") or width)))
            height = max(1, round(float(content.get("height") or height)))
            params.update(
                {
                    "captureBeyondViewport": True,
                    "clip": {"x": 0, "y": 0, "width": width, "height": height, "scale": 1},
                }
            )
        elif clip is not None:
            width = max(1, round(clip.width))
            height = max(1, round(clip.height))
            params["clip"] = {
                "x": clip.x + view.scroll_x,
                "y": clip.y + view.scroll_y,
                "width": clip.width,
                "height": clip.height,
                "scale": 1,
            }
        response = await self._send("Page.captureScreenshot", params)
        return Screenshot(
            data=base64.b64decode(str(response.get("data") or "")),
            format="png",
            width=width,
            height=height,
            device_pixel_ratio=view.device_pixel_ratio,
            full_page=full_page,
        )

    async def frames(self) -> Sequence[FrameInfo]:
        response = await self._send("Page.getFrameTree")
        out: list[FrameInfo] = []

        def visit(node: Mapping[str, Any], parent_id: str = "") -> None:
            frame = node.get("frame") or {}
            frame_id = str(frame.get("id") or "")
            security_origin = str(frame.get("securityOrigin") or "")
            parent_origin = str(frame.get("parentSecurityOrigin") or "")
            out.append(
                FrameInfo(
                    frame_id=frame_id,
                    url=str(frame.get("url") or ""),
                    parent_id=parent_id,
                    is_main=not parent_id,
                    cross_origin=bool(
                        parent_id and parent_origin and security_origin != parent_origin
                    ),
                )
            )
            for child in node.get("childFrames") or ():
                visit(child, frame_id)

        tree = response.get("frameTree")
        if tree:
            visit(tree)
        return out

    # ------------------------------------------------------------------ #
    # Navigation                                                         #
    # ------------------------------------------------------------------ #

    async def _navigation_command(
        self, method: str, params: dict[str, Any] | None, *, timeout: float
    ) -> NavigationResult:
        waiter = self._event_future("Page.loadEventFired", "Page.frameStoppedLoading")
        try:
            response = await self._send(method, params, timeout=timeout)
            if response.get("errorText"):
                raise NavigationError(str(response["errorText"]), method=method)
            await asyncio.wait_for(waiter, timeout=timeout)
        except TimeoutError as exc:
            raise NavigationError(
                f"navigation timed out after {timeout:g}s", method=method
            ) from exc
        except EngineError as exc:
            if isinstance(exc, NavigationError):
                raise
            raise NavigationError(str(exc), method=method) from exc
        finally:
            if not waiter.done():
                waiter.cancel()
        self._generation += 1
        return NavigationResult(url=await self.url(), ok=True)

    async def navigate(self, url: str, *, timeout: float = 30.0) -> NavigationResult:
        return await self._navigation_command("Page.navigate", {"url": url}, timeout=timeout)

    async def reload(self, *, timeout: float = 30.0) -> NavigationResult:
        return await self._navigation_command("Page.reload", {}, timeout=timeout)

    async def _history_move(self, delta: int, *, timeout: float) -> NavigationResult:
        history = await self._send("Page.getNavigationHistory")
        entries = list(history.get("entries") or ())
        position = int(history.get("currentIndex") or 0) + delta
        if not 0 <= position < len(entries):
            return NavigationResult(url=await self.url(), ok=False, error="no history entry")
        return await self._navigation_command(
            "Page.navigateToHistoryEntry",
            {"entryId": entries[position]["id"]},
            timeout=timeout,
        )

    async def go_back(self, *, timeout: float = 30.0) -> NavigationResult:
        return await self._history_move(-1, timeout=timeout)

    async def go_forward(self, *, timeout: float = 30.0) -> NavigationResult:
        return await self._history_move(1, timeout=timeout)

    # ------------------------------------------------------------------ #
    # Input                                                               #
    # ------------------------------------------------------------------ #

    def _element_index(self, element: Element) -> int:
        try:
            generation, index = decode_handle(element.handle)
        except ValueError as exc:
            raise ElementNotFound("malformed handle", handle=element.handle) from exc
        if generation != self._generation:
            raise StaleHandle(
                "handle belongs to a previous page",
                handle=element.handle,
                generation=generation,
                current=self._generation,
            )
        return index

    async def _point_for(self, target: Element | Point) -> Point:
        if isinstance(target, Point):
            return target
        index = self._element_index(target)
        box = await self._read("box", index=index)
        if not box:
            raise StaleHandle("element is no longer in the page", handle=target.handle)
        return Point(
            float(box["x"]) + float(box["width"]) / 2, float(box["y"]) + float(box["height"]) / 2
        )

    @staticmethod
    def _modifier_mask(modifiers: Sequence[KeyModifier]) -> int:
        bits = {
            KeyModifier.ALT: 1,
            KeyModifier.CONTROL: 2,
            KeyModifier.META: 4,
            KeyModifier.SHIFT: 8,
        }
        return sum(bits[item] for item in set(modifiers))

    async def _signature(self) -> str:
        return str(await self._read("signature"))

    async def _settle(self, before: str, changed: str, unchanged: str) -> ActionOutcome:
        for _ in range(6):
            await asyncio.sleep(0.05)
            try:
                after = await self._signature()
            except EngineError:
                return ActionOutcome(ok=True, changed=True, detail="page navigated")
            if after != before:
                return ActionOutcome(ok=True, changed=True, detail=changed)
        return ActionOutcome.no_change(unchanged)

    async def click(
        self,
        target: Element | Point,
        *,
        button: MouseButton = MouseButton.LEFT,
        click_count: int = 1,
        modifiers: Sequence[KeyModifier] = (),
    ) -> ActionOutcome:
        point = await self._point_for(target)
        before = await self._signature()
        common = {
            "x": point.x,
            "y": point.y,
            "button": button.value,
            "clickCount": click_count,
            "modifiers": self._modifier_mask(modifiers),
        }
        await self._send("Input.dispatchMouseEvent", {"type": "mousePressed", **common})
        await self._send("Input.dispatchMouseEvent", {"type": "mouseReleased", **common})
        return await self._settle(before, "click landed", "click hit nothing")

    async def type_text(
        self,
        text: str,
        *,
        target: Element | Point | None = None,
        clear_first: bool = False,
        delay: float = 0.0,
    ) -> ActionOutcome:
        index: int | None = None
        if isinstance(target, Element):
            index = self._element_index(target)
        if target is not None:
            point = await self._point_for(target)
            common = {"x": point.x, "y": point.y, "button": "left", "clickCount": 1}
            await self._send("Input.dispatchMouseEvent", {"type": "mousePressed", **common})
            await self._send("Input.dispatchMouseEvent", {"type": "mouseReleased", **common})
        before = str(await self._read("activeValue"))
        if clear_first:
            cleared = await self._read("clear", **({"index": index} if index is not None else {}))
            if not cleared:
                return ActionOutcome.no_change("the focused element could not be cleared")
        if delay > 0:
            for character in text:
                await self._send("Input.insertText", {"text": character})
                await asyncio.sleep(delay)
        elif text:
            await self._send("Input.insertText", {"text": text})
        landed = str(await self._read("activeValue"))
        expected = text
        verified = landed == text if clear_first else text in landed
        if not verified:
            return ActionOutcome.no_change(
                "typed text did not land in the focused element",
                expected=expected,
                actual=landed,
            )
        return ActionOutcome(
            ok=True,
            changed=landed != before,
            detail="text landed" if landed != before else "typing changed nothing",
            data={"value": landed},
        )

    @staticmethod
    def _key_description(key: str) -> tuple[str, str, int, str]:
        if key in _KEYS:
            return _KEYS[key]
        if len(key) == 1 and key.isprintable():
            upper = key.upper()
            code = f"Key{upper}" if key.isalpha() else (f"Digit{key}" if key.isdigit() else "")
            return key, code, ord(upper), key
        raise ActionFailed("unsupported key", key=key)

    async def press_key(
        self, key: str, *, modifiers: Sequence[KeyModifier] = (), repeat: int = 1
    ) -> ActionOutcome:
        key_name, code, virtual, text = self._key_description(key)
        before = await self._signature()
        common: dict[str, Any] = {
            "key": key_name,
            "code": code,
            "windowsVirtualKeyCode": virtual,
            "nativeVirtualKeyCode": virtual,
            "modifiers": self._modifier_mask(modifiers),
        }
        if text:
            common["text"] = text
            common["unmodifiedText"] = text
        # "keyDown" delivers the character; "rawKeyDown" is explicitly the
        # no-character form. See _KEYS for why sending a text-bearing key the
        # wrong way stalls the next input command rather than this one.
        down = "keyDown" if text else "rawKeyDown"
        for _ in range(max(1, repeat)):
            await self._send("Input.dispatchKeyEvent", {"type": down, **common})
            await self._send("Input.dispatchKeyEvent", {"type": "keyUp", **common})
        return await self._settle(before, f"pressed {key}", f"{key} changed nothing")

    async def scroll(
        self, delta_x: float, delta_y: float, *, at: Point | None = None
    ) -> ActionOutcome:
        limits = await self._read("viewport")
        before = await self.viewport()
        point = at or Point(before.width / 2, before.height / 2)

        # Ask the page whether it can move before telling the browser to move
        # it. Chrome acknowledges a wheel event only once the compositor has
        # handled it, and at the scroll limit that acknowledgement may never
        # arrive -- and because CDP commands for one debuggee are serialised, an
        # unacknowledged wheel blocks every later command behind it. Over the
        # DevTools pipe this is rare enough to look like flakiness; through an
        # extension relay it is reproducible. So the cheap read below is not an
        # optimisation, it is what stops the pipe wedging.
        at_limit = (delta_y > 0 and before.scroll_y >= float(limits.get("maxScrollY", 0)) - 1) or (
            delta_y < 0 and before.scroll_y <= 0
        )
        if at_limit and not delta_x:
            return ActionOutcome.no_change(
                "already at the scroll limit",
                scroll_x=before.scroll_x,
                scroll_y=before.scroll_y,
            )

        await self._send(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseWheel",
                "x": point.x,
                "y": point.y,
                "deltaX": delta_x,
                "deltaY": delta_y,
            },
        )
        after = before
        for _ in range(10):
            await asyncio.sleep(0.03)
            after = await self.viewport()
            if (after.scroll_x, after.scroll_y) != (before.scroll_x, before.scroll_y):
                break
        if (after.scroll_x, after.scroll_y) == (before.scroll_x, before.scroll_y):
            return ActionOutcome.no_change(
                "already at the scroll limit", scroll_x=after.scroll_x, scroll_y=after.scroll_y
            )
        return ActionOutcome(
            ok=True,
            changed=True,
            detail="scrolled",
            data={"scroll_x": after.scroll_x, "scroll_y": after.scroll_y},
        )

    async def hover(self, target: Element | Point) -> ActionOutcome:
        point = await self._point_for(target)
        before = await self._signature()
        await self._send(
            "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": point.x, "y": point.y}
        )
        return await self._settle(before, "hovered", "hover changed nothing")

    async def drag(
        self,
        path: Sequence[Point],
        *,
        button: MouseButton = MouseButton.LEFT,
        hold: float = 0.0,
    ) -> ActionOutcome:
        if len(path) < 2:
            return ActionOutcome.failure("a drag needs at least two points")
        before = await self._signature()
        buttons = {MouseButton.LEFT: 1, MouseButton.RIGHT: 2, MouseButton.MIDDLE: 4}[button]
        first = path[0]
        await self._send(
            "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": first.x, "y": first.y}
        )
        await self._send(
            "Input.dispatchMouseEvent",
            {
                "type": "mousePressed",
                "x": first.x,
                "y": first.y,
                "button": button.value,
                "clickCount": 1,
            },
        )
        if hold > 0:
            await asyncio.sleep(hold)
        previous = first
        for point in path[1:]:
            for step in range(1, 9):
                fraction = step / 8
                x = previous.x + (point.x - previous.x) * fraction
                y = previous.y + (point.y - previous.y) * fraction
                # CDP otherwise defaults to no buttons, silently cancelling HTML5 DnD.
                await self._send(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseMoved",
                        "x": x,
                        "y": y,
                        "button": button.value,
                        "buttons": buttons,
                    },
                )
            previous = point
        last = path[-1]
        await self._send(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseReleased",
                "x": last.x,
                "y": last.y,
                "button": button.value,
                "clickCount": 1,
            },
        )
        return await self._settle(before, "dragged", "the drag moved nothing")

    async def select_option(
        self, target: Element, *, value: str = "", label: str = "", index: int = -1
    ) -> ActionOutcome:
        node = self._element_index(target)
        result = await self._read("select", index=node, value=value, label=label, optionIndex=index)
        if not result or not result.get("ok"):
            return ActionOutcome.failure(
                "no matching option", target=target.description, result=result
            )
        return ActionOutcome(
            ok=True,
            changed=True,
            detail=f"selected {result['value']}",
            data={"value": result["value"], "label": result.get("label", "")},
        )

    async def upload_files(self, target: Element, paths: Sequence[str]) -> ActionOutcome:
        index = self._element_index(target)
        remote = await self._runtime_evaluate(
            f"window.__relaykit && window.__relaykit.nodes[{index}]", return_by_value=False
        )
        object_id = str(remote.get("objectId") or "")
        if not object_id:
            raise StaleHandle("upload target is gone", handle=target.handle)
        # Address the input by objectId rather than nodeId. DOM.requestNode maps
        # a remote object into the DOM agent's node table, which is empty until
        # DOM.getDocument has populated it -- so it answers 0, and the upload
        # looks like a stale element rather than an un-primed agent.
        # setFileInputFiles takes an objectId directly and skips the whole
        # problem.
        await self._send("DOM.setFileInputFiles", {"files": list(paths), "objectId": object_id})
        return ActionOutcome(
            ok=True,
            changed=True,
            detail=f"attached {len(paths)} file(s)",
            data={"count": len(paths)},
        )

    async def set_zoom(self, factor: float) -> ActionOutcome:
        if factor <= 0:
            raise ActionFailed("zoom factor must be positive", factor=factor)
        await self._send("Emulation.setPageScaleFactor", {"pageScaleFactor": factor})
        return ActionOutcome(ok=True, changed=True, detail=f"zoom {factor}")

    # ------------------------------------------------------------------ #
    # Scripting, tabs, dialogs, and cookies                               #
    # ------------------------------------------------------------------ #

    async def evaluate(self, script: str, *args: Any, frame_id: str = "") -> Any:
        if frame_id:
            frames = {frame.frame_id for frame in await self.frames()}
            if frame_id not in frames:
                raise EvaluationError("no such frame", frame_id=frame_id)
            raise CapabilityNotSupported(
                "evaluation in a named frame requires the extension CDP pipe",
                capability=Capability.CROSS_ORIGIN_FRAMES.value,
            )
        expression = script
        if args:
            encoded = ",".join(json.dumps(arg) for arg in args)
            expression = f"({script})({encoded})"
        return await self._runtime_evaluate(expression)

    async def add_init_script(self, script: str) -> None:
        await self._send("Page.addScriptToEvaluateOnNewDocument", {"source": script})

    async def tabs(self) -> Sequence[TabInfo]:
        response = await self._require_connection().send("Target.getTargets")
        return tuple(
            TabInfo(
                tab_id=str(item["targetId"]),
                url=str(item.get("url") or ""),
                title=str(item.get("title") or ""),
                active=str(item["targetId"]) == self._target_id,
                attached=str(item["targetId"]) == self._target_id,
            )
            for item in response.get("targetInfos", ())
            if item.get("type") == "page"
        )

    async def active_tab(self) -> TabInfo:
        for tab in await self.tabs():
            if tab.active:
                return tab
        raise ActionFailed("no active tab")

    async def switch_tab(self, tab_id: str) -> TabInfo:
        available = {tab.tab_id for tab in await self.tabs()}
        if tab_id not in available:
            raise TabNotFound("no such tab", tab_id=tab_id)
        connection = self._require_connection()
        if self._session_id:
            await connection.send("Target.detachFromTarget", {"sessionId": self._session_id})
            self._session_id = ""
        await connection.send("Target.activateTarget", {"targetId": tab_id})
        await self._attach_page(tab_id)
        self._generation += 1
        return await self.active_tab()

    async def open_tab(self, url: str = "") -> TabInfo:
        created = await self._require_connection().send(
            "Target.createTarget", {"url": url or "about:blank"}
        )
        await self.switch_tab(str(created["targetId"]))
        return await self.active_tab()

    async def close_tab(self, tab_id: str) -> None:
        available = {tab.tab_id for tab in await self.tabs()}
        if tab_id not in available:
            raise TabNotFound("no such tab", tab_id=tab_id)
        was_active = tab_id == self._target_id
        response = await self._require_connection().send("Target.closeTarget", {"targetId": tab_id})
        if response.get("success") is False:
            raise ActionFailed("Chrome refused to close the tab", tab_id=tab_id)
        if was_active:
            self._session_id = ""
            self._target_id = ""
            remaining = await self.tabs()
            if remaining:
                await self._attach_page(remaining[0].tab_id)
            self._generation += 1

    async def handle_dialog(self, *, accept: bool, prompt_text: str = "") -> ActionOutcome:
        await self._send(
            "Page.handleJavaScriptDialog",
            {"accept": accept, "promptText": prompt_text},
        )
        return ActionOutcome(ok=True, changed=True, detail="dialog handled")

    async def cookies(self, urls: Sequence[str] = ()) -> Sequence[Mapping[str, Any]]:
        params: dict[str, Any] = {}
        if urls:
            params["urls"] = list(urls)
        response = await self._send("Network.getCookies", params)
        return tuple(response.get("cookies") or ())

    async def set_cookies(self, cookies: Sequence[Mapping[str, Any]]) -> None:
        await self._send("Network.setCookies", {"cookies": [dict(item) for item in cookies]})
