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

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ...core.engine import BrowserEngine, Capabilities, Capability, EngineInfo
from ...core.errors import CapabilityNotSupported, EngineNotAvailable
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
    Viewport,
)
from .bridge import SafariBridge
from .build import engine_app_path

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
        **_options: Any,
    ) -> None:
        self._window = window
        self._bridge = bridge
        self._engine_app = Path(engine_app) if engine_app else None
        self._generation = 0

    # ------------------------------------------------------------------ #
    # Availability                                                        #
    # ------------------------------------------------------------------ #

    @classmethod
    async def probe(cls) -> None:
        if sys.platform != "darwin":
            raise EngineNotAvailable("the Safari engine only runs on macOS")
        if engine_app_path() is None:
            raise EngineNotAvailable(
                "the Safari helper is not built; call "
                "relaykit.engines.safari.build.build_engine() with your app's "
                "bundle identifier",
            )
        raise EngineNotAvailable(
            "the Safari engine needs its Web Extension for perception, which is "
            "not written yet; see docs/porting/safari.md",
            tracking="https://github.com/christopher-inegbedion/relaykit/issues/2",
        )

    @property
    def capabilities(self) -> Capabilities:
        return SUPPORTED_CAPABILITIES

    async def info(self) -> EngineInfo:
        import platform

        version = ""
        if self._bridge is not None:
            with_suppress = await self._bridge.applescript(
                'tell application "Safari" to return version'
            )
            version = with_suppress
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
        await self.probe()  # refuses until the extension exists

    async def close(self) -> None:
        if self._bridge is not None:
            await self._bridge.close()

    # ------------------------------------------------------------------ #
    # Not yet implemented                                                 #
    # ------------------------------------------------------------------ #
    #
    # These are stubs, not design. The mechanism for each is settled and written
    # down in docs/porting/safari.md; what is missing is the extension half that
    # supplies geometry, so there is nothing yet to convert into a page
    # coordinate for the bridge to press.

    def _pending(self) -> CapabilityNotSupported:
        return CapabilityNotSupported("the Safari engine is mid-port; see docs/porting/safari.md")

    async def url(self) -> str:
        raise self._pending()

    async def title(self) -> str:
        raise self._pending()

    async def viewport(self) -> Viewport:
        raise self._pending()

    async def snapshot(self, *, include_text: bool = True) -> Snapshot:
        raise self._pending()

    async def screenshot(self, *, full_page: bool = False, clip: Box | None = None) -> Screenshot:
        raise self._pending()

    async def navigate(self, url: str, *, timeout: float = 30.0) -> NavigationResult:
        raise self._pending()

    async def reload(self, *, timeout: float = 30.0) -> NavigationResult:
        raise self._pending()

    async def go_back(self, *, timeout: float = 30.0) -> NavigationResult:
        raise self._pending()

    async def click(
        self,
        target: Element | Point,
        *,
        button: MouseButton = MouseButton.LEFT,
        click_count: int = 1,
        modifiers: Sequence[KeyModifier] = (),
    ) -> ActionOutcome:
        raise self._pending()

    async def type_text(
        self,
        text: str,
        *,
        target: Element | Point | None = None,
        clear_first: bool = False,
        delay: float = 0.0,
    ) -> ActionOutcome:
        raise self._pending()

    async def press_key(
        self, key: str, *, modifiers: Sequence[KeyModifier] = (), repeat: int = 1
    ) -> ActionOutcome:
        raise self._pending()

    async def scroll(
        self, delta_x: float, delta_y: float, *, at: Point | None = None
    ) -> ActionOutcome:
        raise self._pending()
