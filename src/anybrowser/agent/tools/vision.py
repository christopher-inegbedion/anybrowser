"""Tools for an agent that decides from pixels alone.

They take coordinates instead of handles. A coordinate is a pixel in the
screenshot the planner was shown, and :class:`~anybrowser.agent.vision.VisionPlanner`
always shows the screenshot at the viewport's CSS size -- so a pixel in the
image *is* a viewport point, and these tools pass it straight to the engine.
That contract lives in one place, the planner, rather than in a scale factor
every tool would have to apply and one would eventually forget.

Everything else -- the honest ``changed``, failures as results rather than
crashes -- is the same as the handle tools, because it is the same engine.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from ...core.types import Point
from ..tool import Tool, ToolContext, ToolResult
from .browser import NavigateTool, PressKeyTool, WaitTool, _EngineTool

__all__ = ["VISION_TOOLS", "PointClickTool", "PointScrollTool", "PointTypeTool", "vision_tools"]

_XY: Mapping[str, Any] = {
    "x": {"type": "integer", "description": "Pixels from the left edge of the screenshot."},
    "y": {"type": "integer", "description": "Pixels from the top edge of the screenshot."},
}


async def _point(ctx: ToolContext, x: Any, y: Any) -> Point | ToolResult:
    """A viewport point, or a failed result saying why there is none."""
    try:
        px, py = float(x), float(y)
    except (TypeError, ValueError):
        return ToolResult.failure(f"x and y must be numbers, got x={x!r} y={y!r}")
    view = await ctx.engine.viewport()
    if not (0 <= px < view.width and 0 <= py < view.height):
        return ToolResult.failure(
            f"({px:g}, {py:g}) is outside the {view.width}x{view.height} screenshot"
        )
    return Point(px, py)


class PointClickTool(_EngineTool):
    name = "click"
    description = "Click at a point on the screenshot."
    parameters: ClassVar[Mapping[str, Any]] = {
        "type": "object",
        "properties": dict(_XY),
        "required": ["x", "y"],
    }

    async def _run(self, ctx: ToolContext, x: Any = None, y: Any = None, **_: Any) -> ToolResult:
        point = await _point(ctx, x, y)
        if isinstance(point, ToolResult):
            return point
        outcome = await ctx.engine.click(point)
        where = f"({point.x:g}, {point.y:g})"
        summary = f"clicked {where}" if outcome.changed else f"clicked {where} but nothing changed"
        return ToolResult.of(outcome, summary)


class PointTypeTool(_EngineTool):
    name = "type_text"
    description = (
        "Type text. Give x and y to click a field first; leave them out to type into "
        "whatever already has focus."
    )
    parameters: ClassVar[Mapping[str, Any]] = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            **_XY,
            "clear_first": {"type": "boolean", "default": False},
        },
        "required": ["text"],
    }

    async def _run(
        self,
        ctx: ToolContext,
        text: str = "",
        x: Any = None,
        y: Any = None,
        clear_first: bool = False,
        **_: Any,
    ) -> ToolResult:
        target: Point | None = None
        if x is not None or y is not None:
            point = await _point(ctx, x, y)
            if isinstance(point, ToolResult):
                return point
            target = point
        outcome = await ctx.engine.type_text(text, target=target, clear_first=clear_first)
        summary = f"typed {text!r}" if outcome.changed else "the text did not land in a field"
        return ToolResult.of(outcome, summary)


class PointScrollTool(_EngineTool):
    name = "scroll"
    description = (
        "Scroll. Positive delta_y scrolls down. Give x and y to scroll the part of the "
        "page under that point, such as a panel. A result saying nothing changed means "
        "you have reached the end."
    )
    parameters: ClassVar[Mapping[str, Any]] = {
        "type": "object",
        "properties": {"delta_y": {"type": "number", "default": 400}, **_XY},
    }

    async def _run(
        self, ctx: ToolContext, delta_y: float = 400, x: Any = None, y: Any = None, **_: Any
    ) -> ToolResult:
        at: Point | None = None
        if x is not None and y is not None:
            point = await _point(ctx, x, y)
            if isinstance(point, ToolResult):
                return point
            at = point
        return ToolResult.of(await ctx.engine.scroll(0, float(delta_y), at=at))


#: An image-only agent's whole toolset. Waiting, keys and navigation need no
#: coordinates, so they are the same tools the handle agent uses.
VISION_TOOLS: tuple[type[Tool], ...] = (
    PointClickTool,
    PointTypeTool,
    PressKeyTool,
    PointScrollTool,
    WaitTool,
    NavigateTool,
)


def vision_tools() -> list[Tool]:
    return [cls() for cls in VISION_TOOLS]
