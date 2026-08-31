"""Drive a browser, read the page, click something.

uv run python examples/quickstart.py
uv run python examples/quickstart.py chrome     # your own window, once ported
"""

from __future__ import annotations

import asyncio
import sys

from relaykit import open_engine
from relaykit.core import Capability


async def main(engine_name: str = "playwright") -> None:
    async with await open_engine(engine_name, headless=False) as engine:
        info = await engine.info()
        print(f"driving {info.browser} {info.browser_version} via {info.name}")

        await engine.navigate("https://example.com")
        page = await engine.snapshot()
        print(f"{page.title} — {len(page.elements)} interactive elements")

        for element in page.elements[:5]:
            print(f"  {element.handle:>8}  {element.description}")

        link = next((el for el in page.elements if el.tag == "a"), None)
        if link is None:
            print("nothing to click")
            return

        outcome = await engine.click(link)
        # `changed` is the field that matters: a click that dispatched onto
        # nothing is ok=True, changed=False, and treating that as progress is
        # how agents end up looping. See docs/architecture/truthful-outcomes.md
        print(f"clicked {link.description!r}: ok={outcome.ok} changed={outcome.changed}")
        print(f"now at {await engine.url()}")

        if Capability.FULL_PAGE_SCREENSHOT in engine.capabilities:
            shot = await engine.screenshot(full_page=True)
            print(f"screenshot: {len(shot.data)} bytes, {shot.width}x{shot.height}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "playwright"))
