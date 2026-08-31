"""The smallest possible engine, and how to test it.

Not a real backend -- it drives nothing. It exists to show the shape, and to
show that the conformance suite will refuse to be fooled by it:

    pytest --pyargs relaykit_conformance --engine toy -p examples.custom_engine

fails immediately, because a backend that returns changed=True unconditionally
cannot pass the honesty tests. That is the point.
"""

from __future__ import annotations

from relaykit.core import engines
from relaykit.core.engine import BrowserEngine, Capabilities, Capability, EngineInfo
from relaykit.core.types import (
    ActionOutcome,
    NavigationResult,
    Screenshot,
    Snapshot,
    Viewport,
)


class ToyEngine(BrowserEngine):
    name = "toy"

    def __init__(self, **_options: object) -> None:
        self._url = "about:blank"

    @property
    def capabilities(self) -> Capabilities:
        # Declare only what you have. Under-claiming skips tests; over-claiming
        # fails them. Both are fine; only one is dishonest.
        return Capabilities.of(Capability.TAB_MANAGEMENT)

    async def info(self) -> EngineInfo:
        return EngineInfo(name=self.name, browser="toy", browser_version="0")

    async def start(self) -> None: ...
    async def close(self) -> None: ...

    async def url(self) -> str:
        return self._url

    async def title(self) -> str:
        return "toy"

    async def viewport(self) -> Viewport:
        return Viewport(width=800, height=600)

    async def snapshot(self, *, include_text: bool = True) -> Snapshot:
        return Snapshot(url=self._url, title="toy", viewport=await self.viewport())

    async def screenshot(self, *, full_page: bool = False, clip=None) -> Screenshot:
        return Screenshot(data=b"", width=800, height=600)

    async def navigate(self, url: str, *, timeout: float = 30.0) -> NavigationResult:
        self._url = url
        return NavigationResult(url=url)

    async def reload(self, *, timeout: float = 30.0) -> NavigationResult:
        return NavigationResult(url=self._url)

    async def go_back(self, *, timeout: float = 30.0) -> NavigationResult:
        return NavigationResult(url=self._url)

    async def click(self, target, **kwargs) -> ActionOutcome:
        return ActionOutcome.no_change("the toy engine cannot click anything")

    async def type_text(self, text: str, **kwargs) -> ActionOutcome:
        return ActionOutcome.no_change("the toy engine cannot type")

    async def press_key(self, key: str, **kwargs) -> ActionOutcome:
        return ActionOutcome.no_change("the toy engine has no keyboard")

    async def scroll(self, delta_x: float, delta_y: float, **kwargs) -> ActionOutcome:
        return ActionOutcome.no_change("the toy engine has nothing to scroll")


# In your own package this is an entry point instead; registering in-process is
# the shortcut for tests and examples.
engines.register("toy", ToyEngine)
