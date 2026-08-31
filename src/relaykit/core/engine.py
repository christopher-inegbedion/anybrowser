"""``BrowserEngine`` -- the interface a browser backend implements.

This is the most important file in the project. Everything above it (perception,
the executor, the agent, the daemon) is written against this and nothing else,
which is what makes Chrome, Safari and your own backend interchangeable.

Writing a backend
-----------------
Subclass :class:`BrowserEngine`, implement the abstract methods, declare what you
support in :attr:`BrowserEngine.capabilities`, and register an entry point::

    [project.entry-points."relaykit.engines"]
    firefox = "my_package.engine:FirefoxEngine"

Then run the conformance suite against it::

    pytest --pyargs relaykit_conformance --engine firefox

The suite is the contract. If it passes, the agent runs on your browser.

Why async
---------
Every real backend is asynchronous underneath -- CDP is a WebSocket, the Safari
engine is a line-oriented subprocess, WebDriver is HTTP. A synchronous interface
forces each backend to hide an event loop, and the cost is paid at every layer
above (Relay's original ``PageFacade`` was 1,480 lines of exactly that). Callers
that want blocking calls wrap an engine in
:class:`relaykit.core.sync.SyncEngine`, which owns one loop in one place.

Why capabilities rather than exceptions
--------------------------------------
Backends differ in kind, not just quality. Safari has no CDP and cannot expose a
debugger protocol; a WebDriver backend cannot attach to the user's real window.
Callers need to *plan* around those gaps, not discover them by catching an
exception mid-action. So a backend declares its shape up front and the layers
above route accordingly -- see ``docs/architecture/capabilities.md``.
"""

from __future__ import annotations

import abc
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .errors import CapabilityNotSupported
from .types import (
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

__all__ = ["BrowserEngine", "Capabilities", "Capability", "EngineInfo"]


class Capability(str, Enum):
    """Things a backend may or may not be able to do.

    Only list a capability here when backends genuinely diverge on it. If every
    plausible backend can do it, it belongs in the abstract floor instead.
    """

    #: Drives the user's own already-open window, with their logins and tabs,
    #: rather than a fresh automation profile.
    ATTACH_TO_USER_SESSION = "attach_to_user_session"
    #: Produces events carrying real user activation (window.open, clipboard,
    #: media playback and file pickers all depend on this).
    TRUSTED_INPUT = "trusted_input"
    #: Can act while the browser is backgrounded or occluded, without moving the
    #: physical cursor.
    BACKGROUND_INPUT = "background_input"
    #: Arbitrary JavaScript evaluation in the page.
    EVALUATE_JS = "evaluate_js"
    #: Evaluation inside cross-origin (out-of-process) iframes.
    CROSS_ORIGIN_FRAMES = "cross_origin_frames"
    #: Screenshots of a tab that is not frontmost.
    OFFSCREEN_SCREENSHOT = "offscreen_screenshot"
    #: Beyond-the-fold capture stitched into one image.
    FULL_PAGE_SCREENSHOT = "full_page_screenshot"
    #: Continuous frame streaming (used for cheap change detection).
    SCREENCAST = "screencast"
    #: Synthetic pointer streams good enough for drag, draw and HTML5 DnD.
    POINTER_GESTURES = "pointer_gestures"
    #: Setting files on <input type=file> without a native picker.
    FILE_UPLOAD = "file_upload"
    #: Intercepting or answering native JS dialogs (alert/confirm/prompt).
    JS_DIALOGS = "js_dialogs"
    #: Reading and writing cookies.
    COOKIES = "cookies"
    #: Network request interception.
    NETWORK_INTERCEPTION = "network_interception"
    #: Creating, closing and switching tabs.
    TAB_MANAGEMENT = "tab_management"
    #: Page zoom control.
    PAGE_ZOOM = "page_zoom"
    #: Injecting scripts that run before page scripts on every navigation.
    INIT_SCRIPTS = "init_scripts"


@dataclass(frozen=True, slots=True)
class Capabilities:
    """The set a backend declares. Membership tests are the whole API."""

    supported: frozenset[Capability] = frozenset()
    #: Free-form notes keyed by capability, surfaced in diagnostics. Use it to
    #: explain a *partial* implementation rather than lying in either direction.
    notes: Mapping[Capability, str] = field(default_factory=dict)

    def __contains__(self, cap: object) -> bool:
        return cap in self.supported

    def require(self, cap: Capability, engine_name: str = "engine") -> None:
        """Raise :class:`CapabilityNotSupported` unless ``cap`` is available."""
        if cap not in self.supported:
            note = self.notes.get(cap, "")
            raise CapabilityNotSupported(
                f"{engine_name} does not support {cap.value}",
                capability=cap.value,
                engine=engine_name,
                note=note,
            )

    @classmethod
    def of(cls, *caps: Capability, **notes: str) -> Capabilities:
        return cls(
            supported=frozenset(caps),
            notes={Capability(k): v for k, v in notes.items()},
        )


@dataclass(frozen=True, slots=True)
class EngineInfo:
    """Identity of a running backend, for logs, telemetry and bug reports."""

    name: str
    browser: str = ""
    browser_version: str = ""
    platform: str = ""
    engine_version: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)


class BrowserEngine(abc.ABC):
    """A live connection to one browser.

    Lifecycle::

        engine = FirefoxEngine(...)
        await engine.start()
        try:
            await engine.navigate("https://example.com")
        finally:
            await engine.close()

    An engine addresses **one active tab at a time**. ``switch_tab`` changes
    which. This keeps the interface small; multi-tab orchestration lives above,
    in :mod:`relaykit.daemon`, where it can be shared by every backend.

    Threading: an engine is not thread-safe and belongs to the loop that started
    it. Use one engine per browser, and :class:`relaykit.core.sync.SyncEngine`
    to reach it from synchronous code.
    """

    #: Registry name. Must match the entry-point key.
    name: str = ""

    # ------------------------------------------------------------------ #
    # Identity and lifecycle                                              #
    # ------------------------------------------------------------------ #

    @classmethod
    async def probe(cls) -> None:
        """Raise :class:`~relaykit.core.errors.EngineNotAvailable` if unusable here.

        Cheap and side-effect free -- the registry may call it on several
        candidate backends to pick one. Do not launch a browser from ``probe``.
        The default implementation assumes the backend is always available.
        """
        return None

    @property
    @abc.abstractmethod
    def capabilities(self) -> Capabilities:
        """What this backend can do. Must be answerable before ``start()``."""

    @abc.abstractmethod
    async def info(self) -> EngineInfo:
        """Identity of the connected browser."""

    @abc.abstractmethod
    async def start(self) -> None:
        """Connect to or launch the browser. Idempotent."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Release everything this engine owns. Idempotent, never raises."""

    async def __aenter__(self) -> BrowserEngine:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ------------------------------------------------------------------ #
    # Observation                                                         #
    # ------------------------------------------------------------------ #

    @abc.abstractmethod
    async def url(self) -> str: ...

    @abc.abstractmethod
    async def title(self) -> str: ...

    @abc.abstractmethod
    async def viewport(self) -> Viewport: ...

    @abc.abstractmethod
    async def snapshot(self, *, include_text: bool = True) -> Snapshot:
        """Observe the page: interactive elements, geometry, text.

        This is the one method whose *quality* decides how well an agent runs on
        your backend, and the one the conformance suite tests hardest. Contract:

        * every returned :class:`~relaykit.core.types.Element` is addressable by
          its ``handle`` until the page navigates;
        * ``box`` is in viewport CSS pixels, for elements inside frames too;
        * elements the user cannot see or reach are excluded, with one exception
          -- file inputs, which are routinely zero-sized and still essential.
        """

    @abc.abstractmethod
    async def screenshot(self, *, full_page: bool = False, clip: Box | None = None) -> Screenshot:
        """Capture pixels. ``full_page`` requires ``FULL_PAGE_SCREENSHOT``."""

    async def frames(self) -> Sequence[FrameInfo]:
        """List frames in the active tab. Single-frame backends return the main frame."""
        return ()

    # ------------------------------------------------------------------ #
    # Navigation                                                          #
    # ------------------------------------------------------------------ #

    @abc.abstractmethod
    async def navigate(self, url: str, *, timeout: float = 30.0) -> NavigationResult: ...

    @abc.abstractmethod
    async def reload(self, *, timeout: float = 30.0) -> NavigationResult: ...

    @abc.abstractmethod
    async def go_back(self, *, timeout: float = 30.0) -> NavigationResult: ...

    async def go_forward(self, *, timeout: float = 30.0) -> NavigationResult:
        raise CapabilityNotSupported(f"{self.name} cannot navigate forward")

    # ------------------------------------------------------------------ #
    # Input                                                               #
    # ------------------------------------------------------------------ #

    @abc.abstractmethod
    async def click(
        self,
        target: Element | Point,
        *,
        button: MouseButton = MouseButton.LEFT,
        click_count: int = 1,
        modifiers: Sequence[KeyModifier] = (),
    ) -> ActionOutcome:
        """Click an element or a viewport point.

        Backends that can produce trusted events must do so -- an untrusted
        click silently fails on any page that gates on user activation, and
        reporting that as success is the single most common cause of an agent
        looping. Report ``changed=False`` when nothing happened.
        """

    @abc.abstractmethod
    async def type_text(
        self,
        text: str,
        *,
        target: Element | Point | None = None,
        clear_first: bool = False,
        delay: float = 0.0,
    ) -> ActionOutcome:
        """Type into ``target``, or into whatever currently has focus.

        Must verify the text landed and report ``changed=False`` if it did not.
        Controlled inputs (React and friends) routinely swallow programmatic
        writes; a backend that does not read back will lie here.
        """

    @abc.abstractmethod
    async def press_key(
        self, key: str, *, modifiers: Sequence[KeyModifier] = (), repeat: int = 1
    ) -> ActionOutcome:
        """Press a named key ("Enter", "Escape", "ArrowDown", "a")."""

    @abc.abstractmethod
    async def scroll(
        self, delta_x: float, delta_y: float, *, at: Point | None = None
    ) -> ActionOutcome:
        """Scroll by a delta, at ``at`` if the backend can target a container.

        Report ``changed=False`` when the scroll position did not move -- an
        agent at the bottom of a page needs to learn that from the outcome.
        """

    async def hover(self, target: Element | Point) -> ActionOutcome:
        raise CapabilityNotSupported(f"{self.name} cannot hover")

    async def drag(
        self, path: Sequence[Point], *, button: MouseButton = MouseButton.LEFT, hold: float = 0.0
    ) -> ActionOutcome:
        """Press at ``path[0]``, move through the rest, release at the last.

        Requires ``POINTER_GESTURES``. A backend whose move events omit the
        pressed-button state will appear to work and silently do nothing on
        HTML5 drag-and-drop; the conformance suite checks precisely this.
        """
        raise CapabilityNotSupported(f"{self.name} cannot drag")

    async def select_option(
        self, target: Element, *, value: str = "", label: str = "", index: int = -1
    ) -> ActionOutcome:
        raise CapabilityNotSupported(f"{self.name} cannot select options")

    async def upload_files(self, target: Element, paths: Sequence[str]) -> ActionOutcome:
        raise CapabilityNotSupported(f"{self.name} cannot upload files")

    async def set_zoom(self, factor: float) -> ActionOutcome:
        raise CapabilityNotSupported(f"{self.name} cannot set zoom")

    # ------------------------------------------------------------------ #
    # Scripting                                                           #
    # ------------------------------------------------------------------ #

    async def evaluate(self, script: str, *args: Any, frame_id: str = "") -> Any:
        """Evaluate JavaScript and return a JSON-serialisable result.

        Requires ``EVALUATE_JS``. Perception helpers degrade to accessibility or
        extension paths when a backend lacks it, so this is optional by design.
        """
        raise CapabilityNotSupported(f"{self.name} cannot evaluate JavaScript")

    async def add_init_script(self, script: str) -> None:
        raise CapabilityNotSupported(f"{self.name} cannot add init scripts")

    # ------------------------------------------------------------------ #
    # Tabs                                                                #
    # ------------------------------------------------------------------ #

    async def tabs(self) -> Sequence[TabInfo]:
        raise CapabilityNotSupported(f"{self.name} cannot enumerate tabs")

    async def active_tab(self) -> TabInfo:
        raise CapabilityNotSupported(f"{self.name} cannot enumerate tabs")

    async def switch_tab(self, tab_id: str) -> TabInfo:
        raise CapabilityNotSupported(f"{self.name} cannot switch tabs")

    async def open_tab(self, url: str = "") -> TabInfo:
        raise CapabilityNotSupported(f"{self.name} cannot open tabs")

    async def close_tab(self, tab_id: str) -> None:
        raise CapabilityNotSupported(f"{self.name} cannot close tabs")

    # ------------------------------------------------------------------ #
    # Dialogs and cookies                                                 #
    # ------------------------------------------------------------------ #

    async def handle_dialog(self, *, accept: bool, prompt_text: str = "") -> ActionOutcome:
        raise CapabilityNotSupported(f"{self.name} cannot handle JS dialogs")

    async def cookies(self, urls: Sequence[str] = ()) -> Sequence[Mapping[str, Any]]:
        raise CapabilityNotSupported(f"{self.name} cannot read cookies")

    async def set_cookies(self, cookies: Sequence[Mapping[str, Any]]) -> None:
        raise CapabilityNotSupported(f"{self.name} cannot write cookies")
