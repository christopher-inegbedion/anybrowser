"""The image-only agent: what it is shown, and what its coordinates mean."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from anybrowser.agent import VisionPlanner, vision_tools
from anybrowser.agent.planner import Observation
from anybrowser.agent.tool import ToolContext
from anybrowser.agent.tools import PointClickTool, PointTypeTool
from anybrowser.core.errors import EngineError
from anybrowser.core.types import (
    ActionOutcome,
    Box,
    Element,
    Point,
    Screenshot,
    Snapshot,
    Viewport,
)


def _png(width: int, height: int) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(out, format="PNG")
    return out.getvalue()


def _observation(shot: Screenshot | None) -> Observation:
    page = Snapshot(
        url="https://secret.invalid/admin",
        title="Secret title",
        viewport=Viewport(width=1280, height=800),
        elements=(
            Element(handle="1:0", box=Box(0, 0, 80, 20), tag="button", label="Hidden label"),
        ),
        text="hidden page text",
    )
    return Observation(
        goal="do the thing",
        snapshot=page,
        screenshot=shot,
        history=(),
        notes=(),
        step_index=0,
    )


def test_the_model_is_shown_the_screenshot_and_nothing_from_the_dom():
    """Image-only means image-only. A DOM label leaking into the prompt would
    let the agent pass tasks the bench means to test vision with."""
    planner = VisionPlanner(object(), model="m")  # type: ignore[arg-type]
    shot = Screenshot(data=_png(1280, 800), width=1280, height=800)
    messages = planner._build_messages(_observation(shot), vision_tools())
    text = "\n".join(m.text for m in messages)
    for leak in ("Hidden label", "hidden page text", "secret.invalid", "Secret title", "handle="):
        assert leak not in text
    assert "1280x800" in text
    assert len(messages[-1].images) == 1


def test_a_2x_screenshot_is_shown_at_css_size():
    """On a Retina capture the image is twice the viewport. The model answers
    in the image's pixels and the tools read them as viewport points, so the
    image must be the viewport's size or every click lands twice as far."""
    planner = VisionPlanner(object(), model="m")  # type: ignore[arg-type]
    shot = Screenshot(data=_png(2560, 1600), width=2560, height=1600, device_pixel_ratio=2.0)
    image = planner._build_messages(_observation(shot), vision_tools())[-1].images[0]
    with Image.open(io.BytesIO(image.data)) as shown:
        assert shown.size == (1280, 800)


def test_no_screenshot_is_an_engine_problem_not_a_model_failure():
    planner = VisionPlanner(object(), model="m")  # type: ignore[arg-type]
    with pytest.raises(EngineError):
        planner._build_messages(_observation(None), vision_tools())


class _Engine:
    def __init__(self) -> None:
        self.clicked: list[Point] = []
        self.typed: list[tuple[str, object]] = []

    async def viewport(self) -> Viewport:
        return Viewport(width=1280, height=800)

    async def click(self, target: Point, **_: object) -> ActionOutcome:
        self.clicked.append(target)
        return ActionOutcome(ok=True, changed=True, detail="click landed")

    async def type_text(self, text: str, *, target: object = None, **_: object) -> ActionOutcome:
        self.typed.append((text, target))
        return ActionOutcome(ok=True, changed=True, detail="typed")


async def test_a_click_goes_to_the_pixel_the_model_named():
    engine = _Engine()
    result = await PointClickTool().run(ToolContext(engine=engine), x=412, y=230)  # type: ignore[arg-type]
    assert result.ok and engine.clicked == [Point(412, 230)]


@pytest.mark.parametrize(("x", "y"), [(-1, 10), (1280, 10), (10, 800), ("left", 3)])
async def test_a_point_off_the_screenshot_is_refused_not_clicked(x, y):
    """A model that answers in the wrong coordinate space -- 0-1000, or a
    downscaled image's pixels -- should hear about it, not click somewhere."""
    engine = _Engine()
    result = await PointClickTool().run(ToolContext(engine=engine), x=x, y=y)  # type: ignore[arg-type]
    assert not result.ok and engine.clicked == []


async def test_typing_without_a_point_types_into_the_focused_field():
    engine = _Engine()
    await PointTypeTool().run(ToolContext(engine=engine), text="hi")  # type: ignore[arg-type]
    await PointTypeTool().run(ToolContext(engine=engine), text="yo", x=5, y=6)  # type: ignore[arg-type]
    assert engine.typed == [("hi", None), ("yo", Point(5, 6))]
