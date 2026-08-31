"""Safari engine -- accessibility for input, extension for perception.

Status: **being ported.** The interface, the entry point and the conformance
gate are in place; the implementation lands from Relay's ``core/safari``. Until
then ``probe()`` refuses rather than half-working, so the registry falls through
to another engine instead of failing mid-run.

See ``docs/architecture/safari-engine.md`` for the design and
``docs/porting/safari.md`` for what is left.
"""

from __future__ import annotations

from ...core.engine import BrowserEngine, Capabilities, Capability
from ...core.errors import EngineNotAvailable

__all__ = ["PLANNED_CAPABILITIES", "SafariEngine"]

#: What the finished engine will declare. Written down first so the conformance
#: run is a real gate rather than a description of whatever got built.
PLANNED_CAPABILITIES = Capabilities.of(
    Capability.ATTACH_TO_USER_SESSION,
    Capability.TRUSTED_INPUT,
    Capability.BACKGROUND_INPUT,
    Capability.EVALUATE_JS,
    Capability.OFFSCREEN_SCREENSHOT,
    Capability.FULL_PAGE_SCREENSHOT,
    Capability.POINTER_GESTURES,
    Capability.FILE_UPLOAD,
    Capability.JS_DIALOGS,
    Capability.COOKIES,
    Capability.TAB_MANAGEMENT,
    Capability.PAGE_ZOOM,
)

#: Safari has no CDP: the Web Inspector protocol needs private Apple
#: entitlements, so network interception, screencast and pre-navigation init
#: scripts have no equivalent, and cross-origin frames are reachable only
#: through the extension. Declared absent rather than faked.


class SafariEngine(BrowserEngine):
    name = "safari"

    @classmethod
    async def probe(cls) -> None:
        raise EngineNotAvailable(
            "the Safari engine is not ported yet; see docs/porting/safari.md",
            tracking="https://github.com/relaykit/relaykit/issues/2",
        )

    def __init__(self, **_options: object) -> None:
        raise EngineNotAvailable("the Safari engine is not ported yet")

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
