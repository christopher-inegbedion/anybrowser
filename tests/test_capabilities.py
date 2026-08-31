from __future__ import annotations

import pytest

from relaykit.core.engine import Capabilities, Capability
from relaykit.core.errors import CapabilityNotSupported


def test_membership_is_the_api():
    caps = Capabilities.of(Capability.EVALUATE_JS)
    assert Capability.EVALUATE_JS in caps
    assert Capability.SCREENCAST not in caps


def test_require_names_the_engine_and_the_gap():
    caps = Capabilities.of(Capability.EVALUATE_JS)
    with pytest.raises(CapabilityNotSupported) as excinfo:
        caps.require(Capability.SCREENCAST, "safari")
    rendered = str(excinfo.value)
    assert "safari" in rendered and "screencast" in rendered


def test_notes_survive_to_the_error():
    """Partial support is explained, not silently half-implemented."""
    caps = Capabilities.of(
        Capability.EVALUATE_JS,
        full_page_screenshot="stitched from tiles; fixed headers repeat",
    )
    assert caps.notes[Capability.FULL_PAGE_SCREENSHOT].startswith("stitched")


def test_undeclared_optional_methods_raise_the_shared_error():
    """Every optional method on the base class must raise the routable error.

    A backend that leaves one as NotImplementedError breaks callers that branch
    on CapabilityNotSupported, and the failure only shows on that one method.
    """
    import asyncio

    from relaykit.core.engine import BrowserEngine

    class Minimal(BrowserEngine):
        name = "minimal"

        @property
        def capabilities(self):
            return Capabilities.of()

        async def info(self): ...
        async def start(self) -> None: ...
        async def close(self) -> None: ...
        async def url(self) -> str:
            return ""

        async def title(self) -> str:
            return ""

        async def viewport(self): ...
        async def snapshot(self, *, include_text: bool = True): ...
        async def screenshot(self, *, full_page: bool = False, clip=None): ...
        async def navigate(self, url: str, *, timeout: float = 30.0): ...
        async def reload(self, *, timeout: float = 30.0): ...
        async def go_back(self, *, timeout: float = 30.0): ...
        async def click(self, target, **kwargs): ...
        async def type_text(self, text: str, **kwargs): ...
        async def press_key(self, key: str, **kwargs): ...
        async def scroll(self, delta_x: float, delta_y: float, **kwargs): ...

    engine = Minimal()
    # Built lazily: constructing every coroutine up front leaves the ones after
    # the first raise un-awaited, which pytest reports as a warning storm rather
    # than as the assertion failure it looks like.
    calls = {
        "evaluate": lambda: engine.evaluate("1"),
        "tabs": lambda: engine.tabs(),
        "cookies": lambda: engine.cookies(),
        "set_zoom": lambda: engine.set_zoom(1.0),
        "hover": lambda: engine.hover(None),
        "drag": lambda: engine.drag([]),
        "go_forward": lambda: engine.go_forward(),
        "add_init_script": lambda: engine.add_init_script(""),
        "handle_dialog": lambda: engine.handle_dialog(accept=True),
    }
    for name, make in calls.items():
        with pytest.raises(CapabilityNotSupported, match="minimal"):
            asyncio.run(make())
            pytest.fail(f"{name} did not refuse")
