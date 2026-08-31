"""Chrome engine -- CDP over an extension-owned debugger session.

Status: **being ported.** The interface, the entry point and the conformance
gate are in place; the implementation lands from Relay's ``core/daemon``. Until
then ``probe()`` refuses rather than half-working, so the registry falls through
to another engine instead of failing mid-run.

See ``docs/architecture/chrome-engine.md`` for the design and
``docs/porting/chrome.md`` for what is left.
"""

from __future__ import annotations

from ...core.engine import BrowserEngine, Capabilities, Capability
from ...core.errors import EngineNotAvailable

__all__ = ["PLANNED_CAPABILITIES", "ChromeEngine"]

#: What the finished engine will declare. Written down first so the conformance
#: run is a real gate rather than a description of whatever got built.
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


class ChromeEngine(BrowserEngine):
    name = "chrome"

    @classmethod
    async def probe(cls) -> None:
        raise EngineNotAvailable(
            "the Chrome engine is not ported yet; see docs/porting/chrome.md",
            tracking="https://github.com/relaykit/relaykit/issues/1",
        )

    def __init__(self, **_options: object) -> None:
        raise EngineNotAvailable("the Chrome engine is not ported yet")

    @property
    def capabilities(self) -> Capabilities:
        return PLANNED_CAPABILITIES

    async def info(self):
        raise NotImplementedError

    async def start(self) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    async def url(self) -> str:
        raise NotImplementedError

    async def title(self) -> str:
        raise NotImplementedError

    async def viewport(self):
        raise NotImplementedError

    async def snapshot(self, *, include_text: bool = True):
        raise NotImplementedError

    async def screenshot(self, *, full_page: bool = False, clip=None):
        raise NotImplementedError

    async def navigate(self, url: str, *, timeout: float = 30.0):
        raise NotImplementedError

    async def reload(self, *, timeout: float = 30.0):
        raise NotImplementedError

    async def go_back(self, *, timeout: float = 30.0):
        raise NotImplementedError

    async def click(self, target, **kwargs):
        raise NotImplementedError

    async def type_text(self, text: str, **kwargs):
        raise NotImplementedError

    async def press_key(self, key: str, **kwargs):
        raise NotImplementedError

    async def scroll(self, delta_x: float, delta_y: float, **kwargs):
        raise NotImplementedError
