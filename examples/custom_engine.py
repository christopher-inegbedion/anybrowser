"""The smallest possible engine, and how to test it.

    uv run python examples/custom_engine.py

Not a real backend -- it drives nothing. It exists to show the shape: running
it registers the toy engine, drives it through `open_engine`, and prints what
came back, including an action that honestly reports changed=False. It also
shows that the conformance suite will refuse to be fooled by it:

    pytest --pyargs anybrowser_conformance --engine toy -p examples.custom_engine

fails immediately, because a backend that returns changed=True unconditionally
cannot pass the honesty tests. That is the point.
"""

from __future__ import annotations

import asyncio

from anybrowser import open_engine
from anybrowser.core import engines
from anybrowser.core.engine import BrowserEngine, Capabilities, Capability, EngineInfo
from anybrowser.core.types import (
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


async def main() -> None:
    async with await open_engine("toy") as engine:
        info = await engine.info()
        print(f"driving {info.browser} via {info.name}")

        nav = await engine.navigate("https://example.com")
        print(f"navigated to {nav.url}; now at {await engine.url()}")

        # The toy engine cannot click. The honest answer is a no-op, and
        # `changed=False` is what stops an agent from counting it as progress.
        outcome = await engine.scroll(0, 400)
        print(f"scrolled: ok={outcome.ok} changed={outcome.changed} ({outcome.detail})")


if __name__ == "__main__":
    asyncio.run(main())
