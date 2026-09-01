"""The Safari engine: accessibility for input, an extension for perception.

Safari exposes no CDP. The Web Inspector protocol is gated behind private Apple
entitlements, ``chrome.debugger`` has no counterpart, and WebDriver BiDi is
advertised but non-functional as of Safari 26.5. ``safaridriver`` works well and
is unusable here for one structural reason: it only ever drives its own
Automation window, a clean profile with none of the user's logins, and it cannot
adopt a window that already exists.

So this engine is assembled from parts, each doing what it is best at:

===============  ==========================================  ========================
Layer            Mechanism                                   Gives
===============  ==========================================  ========================
Activation       Accessibility (``AXPress``, ``AXValue``)    trusted input, in the
                                                             background, cursor
                                                             untouched
Perception       Safari Web Extension content script         DOM, geometry, overlay
Gesture          synthetic PointerEvents from that script    drag, draw, canvas
Pixels           ScreenCaptureKit via the helper             occluded windows
Navigation       AppleScript                                 tabs, windows, history
===============  ==========================================  ========================

The transferable idea is the routing one: **driving a browser does not require a
mouse.** Most interaction is better expressed as *activate this element* than as
*click these pixels*, and the activate-form has background-capable
implementations where the pixel-form does not.

Status: the native half (:mod:`.bridge`) is ported and the perception half needs
the Safari Web Extension, which is not written yet. ``probe()`` refuses until
both are present rather than half-working. See ``docs/porting/safari.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ...core.engine import BrowserEngine, Capabilities, Capability, EngineInfo
from ...core.errors import (
    ActionFailed,
    CapabilityNotSupported,
    EngineNotAvailable,
    StaleHandle,
    TabNotFound,
)
from ...core.types import (
    ActionOutcome,
    Box,
    Element,
    KeyModifier,
    MouseButton,
    NavigationResult,
    Point,
    Screenshot,
    Snapshot,
    TabInfo,
    Viewport,
)
from ...perception.dom import build_snapshot, decode_handle
from .bridge import SafariBridge
from .build import engine_app_path, on_macos
from .channel import SafariExtensionChannel

__all__ = ["SUPPORTED_CAPABILITIES", "SafariEngine"]

#: What Safari can actually do.
#:
#: The absences are the interesting part, and they are structural rather than
#: unfinished. Without the Inspector protocol there is nothing to implement
#: network interception, screencast, or pre-navigation init scripts *with*, and
#: cross-origin frames are reachable only through the extension. Declaring them
#: absent is what lets a planner route around them instead of discovering the
#: gap mid-task -- see docs/architecture/capabilities.md.
SUPPORTED_CAPABILITIES = Capabilities.of(
    Capability.ATTACH_TO_USER_SESSION,
    Capability.TRUSTED_INPUT,
    Capability.BACKGROUND_INPUT,
    Capability.EVALUATE_JS,
    Capability.OFFSCREEN_SCREENSHOT,
    Capability.POINTER_GESTURES,
    Capability.FILE_UPLOAD,
    Capability.JS_DIALOGS,
    Capability.COOKIES,
    Capability.TAB_MANAGEMENT,
    Capability.PAGE_ZOOM,
    evaluate_js="through the extension's content script, not a debugger protocol",
    trusted_input="AXPress and AXValue; synthetic PointerEvents for gestures",
    offscreen_screenshot="ScreenCaptureKit; needs a Screen Recording grant",
)


class SafariEngine(BrowserEngine):
    """Drive the user's own Safari window."""

    name = "safari"

    def __init__(
        self,
        *,
        engine_app: str | Path = "",
        window: str = "",
        bridge: SafariBridge | None = None,
        channel: SafariExtensionChannel | None = None,
        extension_port: int = 8788,
        connect_timeout: float = 60.0,
        **_options: Any,
    ) -> None:
        self._window = window
        self._bridge = bridge
        self._channel = channel
        self._extension_port = extension_port
        self._connect_timeout = connect_timeout
        self._engine_app = Path(engine_app) if engine_app else None
        self._generation = 0
        self._tab_id = ""

    # ------------------------------------------------------------------ #
    # Availability                                                        #
    # ------------------------------------------------------------------ #

    @classmethod
    async def probe(cls) -> None:
        if not on_macos():
            raise EngineNotAvailable("the Safari engine only runs on macOS")
        if engine_app_path() is None:
            raise EngineNotAvailable(
                "the Safari helper is not built; call "
                "relaykit.engines.safari.build.build_engine() with your app's "
                "bundle identifier",
            )

    @property
    def capabilities(self) -> Capabilities:
        return SUPPORTED_CAPABILITIES

    async def info(self) -> EngineInfo:
        import platform

        version = ""
        if self._bridge is not None:
            with contextlib.suppress(Exception):
                version = await self._bridge.applescript(
                    'tell application "Safari" to return version'
                )
        return EngineInfo(
            name=self.name,
            browser="Safari",
            browser_version=version,
            platform=platform.platform(),
            engine_version="0.1.0",
        )

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        await self.probe()
        if self._bridge is None:
            app = engine_app_path()
            if app is None:  # pragma: no cover - probe already checked
                raise EngineNotAvailable("the Safari helper is not built")
            self._bridge = SafariBridge(app)
        status = await self._bridge.status()
        if not status.ax_trusted:
            raise EngineNotAvailable(
                "Accessibility is not granted, so no input can be produced. Approve "
                "the host application in System Settings > Privacy & Security > "
                "Accessibility. If it is not listed, the helper was built with a "
                "different bundle identifier than the app -- see build_engine().",
            )
        if self._channel is None:
            self._channel = SafariExtensionChannel(
                port=self._extension_port, connect_timeout=self._connect_timeout
            )
        await self._channel.start()
        listing = await self._channel.request("tabs")
        tabs = [t for t in listing.get("tabs", ()) if t.get("tab_id")]
        if not tabs:
            raise EngineNotAvailable("Safari reported no tabs")
        chosen = next((t for t in tabs if t.get("active")), tabs[0])
        self._tab_id = str(chosen["tab_id"])

    async def close(self) -> None:
        if self._channel is not None:
            await self._channel.close()
        if self._bridge is not None:
            await self._bridge.close()

    # ------------------------------------------------------------------ #
    # Internals                                                           #
    # ------------------------------------------------------------------ #

    def _require_channel(self) -> SafariExtensionChannel:
        if self._channel is None or not self._channel.connected:
            raise ActionFailed("the Safari extension is not connected")
        return self._channel

    def _require_bridge(self) -> SafariBridge:
        if self._bridge is None:
            raise ActionFailed("the Safari helper is not running")
        return self._bridge

    async def _read(self, op: str, **args: Any) -> Any:
        result = await self._require_channel().request(
            "read", {"tabId": self._tab_id, "op": op, "args": args}
        )
        return result.get("value", result)

    async def _signature(self) -> str:
        with contextlib.suppress(Exception):
            return str(await self._read("signature"))
        return ""

    async def _settle(self, before: str, changed: str, unchanged: str) -> ActionOutcome:
        """Decide honestly whether the page moved. See truthful-outcomes.md."""
        for _ in range(6):
            await asyncio.sleep(0.05)
            if await self._signature() != before:
                return ActionOutcome(ok=True, changed=True, detail=changed)
        return ActionOutcome.no_change(unchanged)

    async def _point_for(self, target: Element | Point) -> Point:
        if isinstance(target, Point):
            return target
        generation, index = decode_handle(target.handle)
        if generation != self._generation:
            raise StaleHandle("handle belongs to a previous page", handle=target.handle)
        box = await self._read("box", index=index)
        if not box:
            raise StaleHandle("element is no longer in the page", handle=target.handle)
        return Point(
            float(box["x"]) + float(box["width"]) / 2,
            float(box["y"]) + float(box["height"]) / 2,
        )

    # ------------------------------------------------------------------ #
    # Observation                                                         #
    # ------------------------------------------------------------------ #

    async def url(self) -> str:
        return str(
            await self._require_channel().request(
                "read", {"tabId": self._tab_id, "op": "href", "args": {}}
            )
            or ""
        )

    async def title(self) -> str:
        listing = await self._require_channel().request("tabs")
        for tab in listing.get("tabs", ()):
            if str(tab.get("tab_id")) == self._tab_id:
                return str(tab.get("title") or "")
        return ""

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
        raw = await self._require_channel().request(
            "perceive", {"tabId": self._tab_id, "includeText": include_text}
        )
        listing = await self._require_channel().request("tabs")
        url = title = ""
        for tab in listing.get("tabs", ()):
            if str(tab.get("tab_id")) == self._tab_id:
                url, title = str(tab.get("url") or ""), str(tab.get("title") or "")
        return build_snapshot(raw, url=url, title=title, generation=self._generation)

    async def screenshot(self, *, full_page: bool = False, clip: Box | None = None) -> Screenshot:
        if full_page:
            self.capabilities.require(Capability.FULL_PAGE_SCREENSHOT, self.name)
        # Pixels come from the native helper rather than the extension:
        # ScreenCaptureKit photographs a window that is occluded or backgrounded,
        # which tabs.captureVisibleTab cannot.
        data = await self._require_bridge().screenshot(window=self._window, crop=True)
        view = await self.viewport()
        return Screenshot(
            data=data,
            format="png",
            width=int(clip.width) if clip else view.width,
            height=int(clip.height) if clip else view.height,
            device_pixel_ratio=view.device_pixel_ratio,
        )

    # ------------------------------------------------------------------ #
    # Navigation                                                          #
    # ------------------------------------------------------------------ #

    async def _navigated(self, url: str) -> NavigationResult:
        self._generation += 1
        for _ in range(40):
            await asyncio.sleep(0.1)
            with contextlib.suppress(Exception):
                current = await self.url()
                if current and current != "about:blank":
                    return NavigationResult(url=current)
        return NavigationResult(url=url)

    async def navigate(self, url: str, *, timeout: float = 30.0) -> NavigationResult:
        await self._require_channel().request(
            "navigate", {"tabId": self._tab_id, "url": url}, timeout=timeout
        )
        return await self._navigated(url)

    async def reload(self, *, timeout: float = 30.0) -> NavigationResult:
        await self._require_bridge().applescript(
            'tell application "Safari" to do JavaScript "location.reload()" in front document'
        )
        return await self._navigated(await self.url())

    async def go_back(self, *, timeout: float = 30.0) -> NavigationResult:
        await self._require_bridge().applescript(
            'tell application "Safari" to do JavaScript "history.back()" in front document'
        )
        return await self._navigated("")

    async def go_forward(self, *, timeout: float = 30.0) -> NavigationResult:
        await self._require_bridge().applescript(
            'tell application "Safari" to do JavaScript "history.forward()" in front document'
        )
        return await self._navigated("")

    # ------------------------------------------------------------------ #
    # Input                                                               #
    # ------------------------------------------------------------------ #

    async def click(
        self,
        target: Element | Point,
        *,
        button: MouseButton = MouseButton.LEFT,
        click_count: int = 1,
        modifiers: Sequence[KeyModifier] = (),
    ) -> ActionOutcome:
        """Click through the accessibility tree, not with synthetic events.

        AXPress produces a trusted event carrying real user activation, in the
        background, without moving the cursor. A synthetic click carries none of
        that and silently does nothing on anything that checks -- window.open,
        clipboard, media, file pickers -- while reporting success.
        """
        point = await self._point_for(target)
        before = await self._signature()
        url = await self.url()
        await self._require_bridge().press(window=self._window, x=point.x, y=point.y, url=url)
        return await self._settle(before, "clicked", "the click changed nothing")

    async def type_text(
        self,
        text: str,
        *,
        target: Element | Point | None = None,
        clear_first: bool = False,
        delay: float = 0.0,
    ) -> ActionOutcome:
        if target is None:
            raise ActionFailed("Safari needs a target to type into")
        point = await self._point_for(target)
        url = await self.url()
        result = await self._require_bridge().fill(
            window=self._window, x=point.x, y=point.y, text=text, url=url
        )
        # The helper writes, reads back, and only escalates to focusing when the
        # write did not take -- so `landed` is measured, not assumed.
        if not result.get("landed", True):
            return ActionOutcome.no_change(
                "the text did not land in the field",
                reason=str(result.get("detail") or ""),
            )
        return ActionOutcome(
            ok=True,
            changed=True,
            detail="text landed",
            data={"raised": result.get("raised", False)},
        )

    async def press_key(
        self, key: str, *, modifiers: Sequence[KeyModifier] = (), repeat: int = 1
    ) -> ActionOutcome:
        combo = " down, ".join(m.value for m in modifiers)
        before = await self._signature()
        script = f'tell application "System Events" to keystroke {json.dumps(key)}'
        if combo:
            script += f" using {{{combo} down}}"
        for _ in range(max(1, repeat)):
            await self._require_bridge().applescript(script)
        return await self._settle(before, f"pressed {key}", f"{key} changed nothing")

    async def scroll(
        self, delta_x: float, delta_y: float, *, at: Point | None = None
    ) -> ActionOutcome:
        before = await self.viewport()
        limits = await self._read("viewport")
        at_limit = (delta_y > 0 and before.scroll_y >= float(limits.get("maxScrollY", 0)) - 1) or (
            delta_y < 0 and before.scroll_y <= 0
        )
        if at_limit and not delta_x:
            return ActionOutcome.no_change(
                "already at the scroll limit",
                scroll_x=before.scroll_x,
                scroll_y=before.scroll_y,
            )
        await self._require_channel().request(
            "read", {"tabId": self._tab_id, "op": "scrollBy", "args": {"x": delta_x, "y": delta_y}}
        )
        for _ in range(10):
            await asyncio.sleep(0.03)
            after = await self.viewport()
            if (after.scroll_x, after.scroll_y) != (before.scroll_x, before.scroll_y):
                return ActionOutcome(
                    ok=True,
                    changed=True,
                    detail="scrolled",
                    data={"scroll_x": after.scroll_x, "scroll_y": after.scroll_y},
                )
        return ActionOutcome.no_change("the page did not scroll")

    async def drag(
        self, path: Sequence[Point], *, button: MouseButton = MouseButton.LEFT, hold: float = 0.0
    ) -> ActionOutcome:
        """Gestures are the one thing synthetic events do better than AX.

        There is no accessibility verb for "drag along this path", and pages
        implement drag with pointer events anyway -- so this is the right layer,
        and `buttons: 1` on every move is what makes it work at all.
        """
        if len(path) < 2:
            return ActionOutcome.failure("a drag needs at least two points")
        events = [{"type": "pointerdown", "x": path[0].x, "y": path[0].y, "buttons": 1}]
        events += [{"type": "pointermove", "x": p.x, "y": p.y, "buttons": 1} for p in path[1:]]
        events.append({"type": "pointerup", "x": path[-1].x, "y": path[-1].y, "buttons": 0})
        before = await self._signature()
        await self._require_channel().request("pointer", {"tabId": self._tab_id, "events": events})
        return await self._settle(before, "dragged", "the drag moved nothing")

    async def hover(self, target: Element | Point) -> ActionOutcome:
        point = await self._point_for(target)
        before = await self._signature()
        await self._require_channel().request(
            "pointer",
            {
                "tabId": self._tab_id,
                "events": [{"type": "pointermove", "x": point.x, "y": point.y, "buttons": 0}],
            },
        )
        return await self._settle(before, "hovered", "hover changed nothing")

    async def select_option(
        self, target: Element, *, value: str = "", label: str = "", index: int = -1
    ) -> ActionOutcome:
        _, node = decode_handle(target.handle)
        result = await self._read("select", index=node, value=value, label=label, optionIndex=index)
        if not result or not result.get("ok"):
            return ActionOutcome.failure(
                "no matching option", available=(result or {}).get("available", [])
            )
        return ActionOutcome(ok=True, changed=True, detail=f"selected {result['value']}")

    # ------------------------------------------------------------------ #
    # Scripting, tabs, dialogs                                            #
    # ------------------------------------------------------------------ #

    async def evaluate(self, script: str, *args: Any, frame_id: str = "") -> Any:
        if frame_id:
            raise CapabilityNotSupported(
                "Safari cannot address a named frame; it has no debugger protocol"
            )
        result = await self._require_channel().request(
            "evaluate", {"tabId": self._tab_id, "script": script}
        )
        return result.get("value")

    async def tabs(self) -> Sequence[TabInfo]:
        listing = await self._require_channel().request("tabs")
        return tuple(
            TabInfo(
                tab_id=str(t.get("tab_id") or ""),
                url=str(t.get("url") or ""),
                title=str(t.get("title") or ""),
                active=bool(t.get("active")),
                window_id=str(t.get("window_id") or ""),
                attached=True,
            )
            for t in listing.get("tabs", ())
        )

    async def active_tab(self) -> TabInfo:
        for tab in await self.tabs():
            if tab.tab_id == self._tab_id:
                return tab
        raise TabNotFound("the attached tab is gone", tab_id=self._tab_id)

    async def switch_tab(self, tab_id: str) -> TabInfo:
        await self._require_channel().request("activate_tab", {"tabId": tab_id})
        self._tab_id = str(tab_id)
        self._generation += 1
        return await self.active_tab()

    async def open_tab(self, url: str = "") -> TabInfo:
        created = await self._require_channel().request("create_tab", {"url": url})
        self._tab_id = str(created["tab_id"])
        self._generation += 1
        return await self.active_tab()

    async def close_tab(self, tab_id: str) -> None:
        await self._require_channel().request("close_tab", {"tabId": tab_id})

    async def handle_dialog(self, *, accept: bool, prompt_text: str = "") -> ActionOutcome:
        """Native dialogs are accessibility objects, so the helper answers them.

        This is Safari's equivalent of CDP's Page.handleJavaScriptDialog.
        """
        result = await self._require_bridge().dialog("accept" if accept else "dismiss")
        return ActionOutcome(
            ok=True, changed=True, detail=str(result.get("detail") or "dialog answered")
        )
