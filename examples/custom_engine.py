"""The smallest possible engine, with a runnable demonstration.

Not a real backend -- it drives nothing. Run this file to see registration,
navigation, snapshots and a truthful no-op outcome without launching a browser:

    python examples/custom_engine.py

Use the conformance suite when developing a real backend:

    pytest --pyargs anybrowser_conformance --engine toy -p examples.custom_engine

The toy engine is only a reference skeleton and does not implement the real
browser behavior required by that suite.
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
        print(f"driving {info.browser} {info.browser_version} via {info.name} (reference skeleton)")
        await engine.navigate("https://example.com")
        page = await engine.snapshot()
        print(f"{page.title} at {page.url}: {len(page.elements)} interactive elements")
        outcome = await engine.click("button")
        print(f"click: ok={outcome.ok} changed={outcome.changed}: {outcome.detail}")
        print("No real browser was opened; implement a backend before running conformance tests.")


if __name__ == "__main__":
    asyncio.run(main())
