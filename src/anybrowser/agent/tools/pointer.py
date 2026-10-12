"""Tools for a two-part agent: a planner names the *item*, a pointer finds the pixel.

    from anypointer import Pointer            # pip install "anybrowser[anypointer]"
    pointer = Pointer()
    planner = VisionPlanner(provider, model=..., system_prompt=ITEM_PROMPT, compact=True)
    runner = AgentRunner(engine, planner, tools=pointer_tools(pointer))

The planner never writes coordinates. ``click_item("the wifi toggle")`` takes a screenshot,
asks the pointer where that is, scales the answer from the screenshot to the viewport, and clicks.
A confidence under ``min_confidence`` is reported back as a failure instead of clicked, so the
planner can describe the item differently.

Any object with ``point(image_bytes, item) -> p`` works as the pointer, where ``p`` has ``x``,
``y``, ``confidence`` and ``image_size`` (``anypointer.Pointer`` does). Its calls are blocking,
so they run in a worker thread.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, ClassVar, Protocol

from ...core.types import Point
from ..tool import Tool, ToolContext, ToolResult
from .browser import NavigateTool, PressKeyTool, WaitTool, _EngineTool
from .vision import PointScrollTool

__all__ = ["ITEM_PROMPT", "ClickItemTool", "PointerLike", "TypeIntoItemTool", "pointer_tools"]

#: The planner's system prompt for item mode. The pointer is asked for the item as it looks;
#: resolving "the cheapest one" into something visible is the planner's job.
ITEM_PROMPT = """\
You operate a web browser from screenshots to achieve a goal. Each turn, choose ONE action.
You never give coordinates. Name the item to act on as it LOOKS on screen, by its visible text, \
icon or position next to something visible: "the Save button", "the wifi toggle", \
"Add to cart under Wool rug". Do not describe what you want to happen, and do not say \
"cheapest" or "third": find that item yourself and name what is written on it. \
A grid cell may be named by its code, like "C27" or "e4".
If an action had [no effect] or the item was not found, name it differently or do something else. \
If the page is still loading, wait.
Finish only when the screenshot shows the goal achieved, with what you found in answer.
Tools: click(item) | type_text(text,item,clear_first) | press_key(key) | \
scroll(delta_y) | wait(seconds<=15) | navigate(url)
Reply as JSON: {"narrative": short reason, "done": true|false, \
"answer": the result when done else "", "tool": name, "arguments": {...}}\
"""


class PointerLike(Protocol):
    def point(self, image: bytes, item: str) -> Any: ...


_ITEM: Mapping[str, Any] = {
    "item": {"type": "string", "description": "The item as it looks on screen: its text, icon, or what it is next to."},
}


class _ItemTool(_EngineTool):
    def __init__(self, pointer: PointerLike, min_confidence: float = 0.0) -> None:
        self._pointer = pointer
        self._min = min_confidence

    async def _locate(self, ctx: ToolContext, item: Any) -> Point | ToolResult:
        if not isinstance(item, str) or not item.strip():
            return ToolResult.failure("name the item to act on")
        shot = await ctx.engine.screenshot()
        view = await ctx.engine.viewport()
        found = await asyncio.to_thread(self._pointer.point, shot.data, item)
        if found.confidence < self._min:
            return ToolResult.failure(
                f"could not find {item!r} on the screen (confidence {found.confidence:.2f}); "
                "name it by its visible text or by what it is next to"
            )
        w, h = found.image_size
        # the pointer answers in screenshot pixels; the viewport may be smaller (device pixel ratio)
        x, y = found.x * view.width / w, found.y * view.height / h
        return Point(min(max(x, 0.0), view.width - 1), min(max(y, 0.0), view.height - 1))


class ClickItemTool(_ItemTool):
    name = "click"
    description = "Click the item you name, as it looks on the screen."
    parameters: ClassVar[Mapping[str, Any]] = {
        "type": "object", "properties": dict(_ITEM), "required": ["item"]}

    async def _run(self, ctx: ToolContext, item: Any = None, **_: Any) -> ToolResult:
        point = await self._locate(ctx, item)
        if isinstance(point, ToolResult):
            return point
        outcome = await ctx.engine.click(point)
        where = f"{item!r} at ({point.x:.0f}, {point.y:.0f})"
        return ToolResult.of(outcome, f"clicked {where}" if outcome.changed
                             else f"clicked {where} but nothing changed")


class TypeIntoItemTool(_ItemTool):
    name = "type_text"
    description = ("Type text. Name the field as item to click it first; leave item out to type into "
                   "whatever already has focus.")
    parameters: ClassVar[Mapping[str, Any]] = {
        "type": "object",
        "properties": {"text": {"type": "string"}, **_ITEM, "clear_first": {"type": "boolean", "default": False}},
        "required": ["text"],
    }

    async def _run(self, ctx: ToolContext, text: str = "", item: Any = None, clear_first: bool = False,
                   **_: Any) -> ToolResult:
        target: Point | None = None
        if item:
            found = await self._locate(ctx, item)
            if isinstance(found, ToolResult):
                return found
            target = found
        outcome = await ctx.engine.type_text(text, target=target, clear_first=clear_first)
        return ToolResult.of(outcome, f"typed {text!r}" if outcome.changed else "the text did not land in a field")


def pointer_tools(pointer: PointerLike | None = None, *, min_confidence: float = 0.0) -> list[Tool]:
    """An item-pointing agent's toolset. ``pointer`` defaults to ``anypointer.Pointer()``."""
    if pointer is None:
        try:
            from anypointer import Pointer
        except ImportError as exc:
            raise ImportError('item pointing needs anypointer: pip install "anybrowser[anypointer]"') from exc
        pointer = Pointer()
    return [ClickItemTool(pointer, min_confidence), TypeIntoItemTool(pointer, min_confidence),
            PressKeyTool(), PointScrollTool(), WaitTool(), NavigateTool()]
