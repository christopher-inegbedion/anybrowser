"""An agent that decides from the screenshot alone.

    planner = VisionPlanner(provider, model="qwen2.5vl:3b")
    runner = AgentRunner(engine, planner, tools=vision_tools())

The model is shown the goal, its own history and one screenshot -- no element
list, no page text, no URL. It answers with pixel coordinates in that
screenshot. This is the agent for pages the DOM does not describe, like a
canvas, and for models that are meant to see rather than read.

See ADR-0008 for why it is pixels and not normalised coordinates: the one small
local model that could point at all scored 0/32 when asked for 0-1000 and 30/32
when asked for pixels.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from typing import Any

from ..core.errors import EngineError
from ..core.types import Screenshot, Viewport
from ..models.provider import ImagePart, Message, ModelProvider, Role
from .llm_planner import LLMPlanner
from .planner import Observation
from .runner import render_history
from .tool import Tool

__all__ = ["VISION_SYSTEM_PROMPT", "VisionPlanner"]

VISION_SYSTEM_PROMPT = """\
You are using a web browser to accomplish a goal. You see the browser only as \
a screenshot. Everything you know about the page comes from that image.

Choose exactly ONE action per turn. Reply with one JSON object and nothing else:

{"tool": "<tool name>", "arguments": {...}, "narrative": "<why, in one sentence>"}

For example, to click a button whose centre is 412 pixels from the left and \
230 from the top of the screenshot:

{"tool": "click", "arguments": {"x": 412, "y": 230}, "narrative": "this is the login button"}

When the goal is achieved, reply instead with:

{"done": true, "answer": "<what you found, or what you did>", "narrative": "<why you are finished>"}

Rules that matter:

- Coordinates are pixels in the screenshot, counted from its top-left corner. \
Aim for the middle of what you want to click.
- To fill in a field, use type_text with the field's x and y; it clicks the \
field and types.
- A result marked [no effect] means the action changed nothing. Doing it again \
will change nothing again: aim somewhere else, scroll, or do something different.
- If the page is still loading -- a spinner, results not in yet -- wait for it \
rather than acting again.
- You get a fresh screenshot before every decision.
- When the screenshot shows what the goal asks for, answer at once. Do not keep \
acting once you can see the answer.
- "narrative" is required, always, and must say why you chose this action.
- Do not claim the goal is done without seeing evidence of it in the screenshot.\
"""


class VisionPlanner(LLMPlanner):
    """:class:`LLMPlanner` with the page shown only as an image.

    The screenshot is resized to the viewport's CSS size before the model sees
    it. On a 2x display the raw capture is twice that, and a model would answer
    in the wrong space; resizing here means a pixel in the image is a viewport
    point, which is what :mod:`anybrowser.agent.tools.vision` expects.
    """

    name = "vision"

    def __init__(
        self,
        provider: ModelProvider,
        *,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        system_prompt: str = VISION_SYSTEM_PROMPT,
        constrained: bool = False,
    ) -> None:
        super().__init__(
            provider,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            system_prompt=system_prompt,
            constrained=constrained,
        )

    def _constrain(
        self, name: str, spec: dict[str, Any], observation: Observation
    ) -> dict[str, Any] | None:
        """Coordinates are integers inside the screenshot. qwen2.5vl once sent
        ``x: [999, 284]`` -- a point packed into one field -- three times running."""
        view = observation.snapshot.viewport
        if name in ("x", "y"):
            limit = (view.width if name == "x" else view.height) - 1
            return {"type": "integer", "minimum": 0, "maximum": int(limit)}
        return super()._constrain(name, spec, observation)

    def _build_messages(self, observation: Observation, tools: Sequence[Tool]) -> list[Message]:
        if observation.screenshot is None:
            # The runner degrades to no screenshot when capture fails, which suits
            # an agent that can read the DOM. This one cannot: with no image it
            # has nothing to decide from. An engine problem, not the model's --
            # so EngineError, which the bench excludes rather than scores.
            raise EngineError(
                "the vision planner needs a screenshot, and none was taken; "
                "run it with RunConfig(include_screenshot=True)"
            )
        view = observation.snapshot.viewport
        image = _at_css_size(observation.screenshot, view)
        parts = [
            f"GOAL: {observation.goal}",
            "",
            "TOOLS:",
            self._render_tools(tools),
            "",
            f"SCREEN: the attached screenshot is {view.width}x{view.height} pixels.",
        ]
        if observation.history:
            reads = {tool.name for tool in tools if not tool.mutating}
            parts += ["", "WHAT YOU HAVE DONE:", render_history(observation.history, reads=reads)]
        if observation.notes:
            parts += ["", "NOTES:", *(f"- {n}" for n in observation.notes)]
        parts += ["", f"Step {observation.step_index + 1}. Choose one action."]
        return [
            Message(role=Role.SYSTEM, text=self._system_prompt),
            Message(role=Role.USER, text="\n".join(parts), images=[image]),
        ]


def _at_css_size(shot: Screenshot, view: Viewport) -> ImagePart:
    if (shot.width, shot.height) == (view.width, view.height) or not (shot.width and view.width):
        return ImagePart(data=shot.data, media_type=f"image/{shot.format}")
    from PIL import Image

    with Image.open(io.BytesIO(shot.data)) as img:
        resized = img.resize((int(view.width), int(view.height)), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        resized.save(out, format="PNG")
    return ImagePart(data=out.getvalue(), media_type="image/png")
