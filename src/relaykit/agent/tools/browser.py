"""The tools an agent uses to drive a page.

One tool per thing the model can decide to do. They are thin on purpose: the
engine already decided what a click means and already reports honestly whether
anything changed, so a tool's job is to name the capability, describe it for a
model, and pass the outcome up without editorialising.

The passing-up is the part that matters. ``ToolResult.changed`` carries the
engine's honest answer into agent history, and history that renders a no-op as
"success" teaches the model that repeating it is progress. See
``docs/architecture/truthful-outcomes.md``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from ...core.errors import CapabilityNotSupported, RelayKitError
from ..tool import Tool, ToolContext, ToolResult

__all__ = [
    "BROWSER_TOOLS",
    "ClickTool",
    "NavigateTool",
    "PressKeyTool",
    "ScrollTool",
    "SelectOptionTool",
    "SnapshotTool",
    "TypeTool",
    "UploadTool",
    "default_tools",
]


class _EngineTool(Tool):
    """Shared plumbing: turn engine errors into results rather than crashes.

    A tool that raises kills the run. A tool that returns a failed
    ``ToolResult`` gives the planner something to read and route around, which
    is almost always what should happen -- a stale handle means re-snapshot, not
    abort.
    """

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            return await self._run(ctx, **kwargs)
        except CapabilityNotSupported as exc:
            return ToolResult.failure(f"this browser cannot do that: {exc}")
        except RelayKitError as exc:
            return ToolResult.failure(str(exc), error_type=type(exc).__name__)

    async def _run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        raise NotImplementedError


class ClickTool(_EngineTool):
    name = "click"
    description = (
        "Click an element on the page. Pass the handle of an element from the current snapshot."
    )
    parameters: ClassVar[Mapping[str, Any]] = {
        "type": "object",
        "properties": {
            "handle": {
                "type": "string",
                "description": (
                    "The opaque id shown as handle=<id> in the page listing, "
                    "e.g. '3:12'. Not the element's label or tag."
                ),
            }
        },
        "required": ["handle"],
    }

    async def _run(self, ctx: ToolContext, handle: str = "", **_: Any) -> ToolResult:
        element = ctx.element(handle)
        outcome = await ctx.engine.click(element)
        summary = (
            f"clicked {element.description!r}"
            if outcome.changed
            else f"clicked {element.description!r} but the page did not change"
        )
        return ToolResult.of(outcome, summary)


class TypeTool(_EngineTool):
    name = "type_text"
    description = "Type text into a field. Set clear_first to replace what is already there."
    parameters: ClassVar[Mapping[str, Any]] = {
        "type": "object",
        "properties": {
            "handle": {"type": "string"},
            "text": {"type": "string"},
            "clear_first": {"type": "boolean", "default": False},
        },
        "required": ["handle", "text"],
    }

    async def _run(
        self,
        ctx: ToolContext,
        handle: str = "",
        text: str = "",
        clear_first: bool = False,
        **_: Any,
    ) -> ToolResult:
        element = ctx.element(handle)
        outcome = await ctx.engine.type_text(text, target=element, clear_first=clear_first)
        summary = (
            f"typed into {element.description!r}"
            if outcome.changed
            else f"the text did not land in {element.description!r}"
        )
        return ToolResult.of(outcome, summary)


class PressKeyTool(_EngineTool):
    name = "press_key"
    description = "Press a key such as Enter, Escape, Tab, or an arrow key."
    parameters: ClassVar[Mapping[str, Any]] = {
        "type": "object",
        "properties": {"key": {"type": "string"}},
        "required": ["key"],
    }

    async def _run(self, ctx: ToolContext, key: str = "", **_: Any) -> ToolResult:
        return ToolResult.of(await ctx.engine.press_key(key), f"pressed {key}")


class ScrollTool(_EngineTool):
    name = "scroll"
    description = (
        "Scroll the page. Positive delta_y scrolls down. A result saying nothing "
        "changed means you have reached the end -- scrolling again will not help."
    )
    parameters: ClassVar[Mapping[str, Any]] = {
        "type": "object",
        "properties": {
            "delta_y": {"type": "number", "default": 400},
            "delta_x": {"type": "number", "default": 0},
        },
    }

    async def _run(
        self, ctx: ToolContext, delta_y: float = 400, delta_x: float = 0, **_: Any
    ) -> ToolResult:
        outcome = await ctx.engine.scroll(delta_x, delta_y)
        return ToolResult.of(outcome)


class NavigateTool(_EngineTool):
    name = "navigate"
    description = "Go to a URL."
    parameters: ClassVar[Mapping[str, Any]] = {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    }

    async def _run(self, ctx: ToolContext, url: str = "", **_: Any) -> ToolResult:
        result = await ctx.engine.navigate(url)
        if not result.ok:
            return ToolResult.failure(f"could not open {url}: {result.error}")
        return ToolResult(ok=True, changed=True, summary=f"opened {result.url}")


class SelectOptionTool(_EngineTool):
    name = "select_option"
    description = "Choose an option in a dropdown, by its visible label."
    parameters: ClassVar[Mapping[str, Any]] = {
        "type": "object",
        "properties": {"handle": {"type": "string"}, "label": {"type": "string"}},
        "required": ["handle", "label"],
    }

    async def _run(
        self, ctx: ToolContext, handle: str = "", label: str = "", **_: Any
    ) -> ToolResult:
        outcome = await ctx.engine.select_option(ctx.element(handle), label=label)
        return ToolResult.of(outcome)


class UploadTool(_EngineTool):
    name = "upload_file"
    description = "Attach a local file to a file input."
    parameters: ClassVar[Mapping[str, Any]] = {
        "type": "object",
        "properties": {"handle": {"type": "string"}, "path": {"type": "string"}},
        "required": ["handle", "path"],
    }

    async def _run(
        self, ctx: ToolContext, handle: str = "", path: str = "", **_: Any
    ) -> ToolResult:
        outcome = await ctx.engine.upload_files(ctx.element(handle), [path])
        return ToolResult.of(outcome)


class SnapshotTool(_EngineTool):
    name = "look"
    description = (
        "Re-read the page. Use this after something changed and you need current element handles."
    )
    parameters: ClassVar[Mapping[str, Any]] = {"type": "object", "properties": {}}
    mutating = False

    async def _run(self, ctx: ToolContext, **_: Any) -> ToolResult:
        page = await ctx.engine.snapshot()
        ctx.snapshot = page
        return ToolResult(
            ok=True,
            changed=False,
            summary=f"{page.title} — {len(page.elements)} elements",
            data={"url": page.url, "element_count": len(page.elements)},
        )


#: Everything a browsing agent needs, in the order a planner tends to reach for.
BROWSER_TOOLS: tuple[type[Tool], ...] = (
    SnapshotTool,
    ClickTool,
    TypeTool,
    PressKeyTool,
    ScrollTool,
    NavigateTool,
    SelectOptionTool,
    UploadTool,
)


def default_tools() -> list[Tool]:
    """One instance of each browser tool."""
    return [cls() for cls in BROWSER_TOOLS]
