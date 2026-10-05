"""Can a model point at a named thing on a screenshot? Measured, not assumed.

The suite grades whole attempts; this grades one skill underneath them. A
vision agent that cannot aim fails every task for a reason no task outcome
names, so this is the first thing to check about a new model -- and the
cheapest number to watch while fine-tuning one, since it needs no browser
loop, just images.

Targets come from the bench's own pages: links, fields and buttons, where the
DOM gives the true box, and canvas-menu buttons, where the drawing code does.
A point is a hit if it lands inside the box.

    python -m anybrowser_bench.grounding --model openai --model-name qwen2.5vl:3b \\
        --model-option base_url=http://localhost:11434/v1 --model-option api_key=ollama

Measured on an M4 at 1280x800: qwen2.5vl:3b 30/32 in pixels and 0/32 in
0-1000 coordinates; qwen3-vl:4b 27/32 at 27s a call; gemma3:4b 2/32.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from random import Random
from typing import Any

from anybrowser.core.engine import BrowserEngine
from anybrowser.core.types import Box

from .server import BenchServer
from .tasks import task

__all__ = ["GroundingReport", "Pointer", "Target", "collect_targets", "llm_pointer", "score"]

#: (png, description, width, height) -> a point in that image's pixels, or None.
Pointer = Callable[[bytes, str, int, int], Awaitable["tuple[float, float] | None"]]

#: The canvas pages' body margin; canvas coordinates are offset by it on screen.
_CANVAS_MARGIN = 24


@dataclass(frozen=True, slots=True)
class Target:
    image: bytes
    width: int
    height: int
    description: str
    box: Box
    kind: str  # "dom" or "canvas"


@dataclass(slots=True)
class GroundingReport:
    hits: dict[str, list[int]] = field(default_factory=lambda: {"dom": [0, 0], "canvas": [0, 0]})
    misses: list[str] = field(default_factory=list)
    seconds: list[float] = field(default_factory=list)

    @property
    def hit_rate(self) -> float:
        got = sum(h[0] for h in self.hits.values())
        total = sum(h[1] for h in self.hits.values())
        return got / total if total else 0.0

    def summary(self) -> str:
        got = sum(h[0] for h in self.hits.values())
        total = sum(h[1] for h in self.hits.values())
        per_call = sum(self.seconds) / len(self.seconds) if self.seconds else 0.0
        return (
            f"{got}/{total} hit ({self.hit_rate:.0%})   "
            f"dom {self.hits['dom'][0]}/{self.hits['dom'][1]}   "
            f"canvas {self.hits['canvas'][0]}/{self.hits['canvas'][1]}   "
            f"{per_call:.1f}s/call"
        )


async def collect_targets(engine: BrowserEngine, seeds: Sequence[int] = (0, 1)) -> list[Target]:
    """Screenshot the bench pages and name what is on them."""
    targets: list[Target] = []

    async def shoot(
        instance: Any, wanted: Sequence[tuple[str, str]] = (), canvas: bool = False
    ) -> None:
        await engine.navigate(server.mount(instance))
        shot = await engine.screenshot()
        found: list[tuple[str, Box]] = []
        if canvas:
            for label, x, y, w, h in re.findall(
                r"\['(\w+)',(\d+),(\d+),(\d+),(\d+),'#", instance.pages["index.html"]
            ):
                box = Box(_CANVAS_MARGIN + int(x), _CANVAS_MARGIN + int(y), int(w), int(h))
                found.append((f"the '{label}' button", box))
        else:
            page = await engine.snapshot()
            for label, description in wanted:
                el = next(
                    (
                        e
                        for e in page.elements
                        if (e.label or "").strip() == label or (e.placeholder or "") == label
                    ),
                    None,
                )
                if el is not None and el.box.width > 0:
                    found.append((description, el.box))
        for description, box in found:
            targets.append(
                Target(
                    shot.data,
                    shot.width,
                    shot.height,
                    description,
                    box,
                    "canvas" if canvas else "dom",
                )
            )

    with BenchServer() as server:
        for seed in seeds:
            directory = task("navigate-detail").build(seed)
            names = re.findall(r"person\d+\.html'>([^<]+)<", directory.pages["index.html"])
            await shoot(directory, [(n, f"the link '{n}'") for n in Random(seed).sample(names, 3)])
            await shoot(
                task("form-fill").build(seed),
                [
                    ("Full name", "the 'Full name' text field"),
                    ("Email", "the 'Email' text field"),
                    ("Create account", "the 'Create account' button"),
                ],
            )
            await shoot(
                task("cookie-wall").build(seed),
                [
                    ("Accept all", "the 'Accept all' button"),
                    ("Reject all", "the 'Reject all' button"),
                ],
            )
            await shoot(
                task("delayed-search").build(seed),
                [("Search documents", "the search input box"), ("Search", "the 'Search' button")],
            )
            await shoot(
                task("pagination").build(seed),
                [("Next page", "the 'Next page' link"), ("3", "the page '3' link")],
            )
            await shoot(task("canvas-menu").build(seed), canvas=True)
    return targets


async def score(
    pointer: Pointer, targets: Sequence[Target], *, slack: float = 4.0
) -> GroundingReport:
    """Ask ``pointer`` for every target. ``slack`` forgives a few pixels at the
    edge of a box, where a click still lands on the element in practice."""
    report = GroundingReport()
    for t in targets:
        started = time.monotonic()
        point = await pointer(t.image, t.description, t.width, t.height)
        report.seconds.append(time.monotonic() - started)
        report.hits[t.kind][1] += 1
        b = t.box
        if point is None:
            report.misses.append(f"{t.description}: no point")
            continue
        x, y = point
        if b.x - slack <= x <= b.x + b.width + slack and b.y - slack <= y <= b.y + b.height + slack:
            report.hits[t.kind][0] += 1
        else:
            report.misses.append(
                f"{t.description}: ({x:.0f},{y:.0f}), box ({b.x:.0f},{b.y:.0f} {b.width:.0f}x{b.height:.0f})"
            )
    return report


def parse_point(text: str) -> tuple[float, float] | None:
    """The first point in a reply: an {"x","y"} pair, or a box's centre."""
    nums = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", text)]
    if len(nums) >= 4 and "[" in text:
        return (nums[0] + nums[2]) / 2, (nums[1] + nums[3]) / 2
    if len(nums) >= 2:
        return nums[0], nums[1]
    return None


def llm_pointer(
    provider: Any,
    model: str,
    *,
    normalized: bool = False,
    max_tokens: int = 2000,
    image_width: int | None = None,
    prompt_tokens: list[int] | None = None,
) -> Pointer:
    """A Pointer backed by any anybrowser model provider.

    ``normalized`` asks for 0-1000 coordinates and scales them back; the default
    is pixels, which is what qwen2.5vl was trained on and the only form it got
    right. ``image_width`` shrinks the screenshot to that width before the
    model sees it and scales its answer back up -- the measurement behind
    choosing a cheaper image. ``prompt_tokens`` collects each call's input size.
    """
    from anybrowser.models.provider import ImagePart, Message, Role

    async def point(
        png: bytes, description: str, width: int, height: int
    ) -> tuple[float, float] | None:
        scale = 1.0
        if image_width and image_width < width:
            png, width, height, scale = _shrink(png, width, height, image_width)
        if normalized:
            ask = (
                f"Find {description} in this screenshot. Reply with only JSON "
                '{"x": X, "y": Y} giving the point to click, in coordinates normalized '
                "to 0-1000 on each axis (0,0 top-left)."
            )
        else:
            ask = (
                f"This screenshot is {width}x{height} pixels. Find {description}. Reply "
                'with only JSON {"x": X, "y": Y} giving the pixel to click (0,0 top-left).'
            )
        reply = await provider.complete(
            [Message(role=Role.USER, text=ask, images=[ImagePart(data=png)])],
            model=model,
            temperature=0.0,
            max_tokens=max_tokens,
        )
        if prompt_tokens is not None:
            prompt_tokens.append(reply.usage.input_tokens)
        p = parse_point(reply.text)
        if p is not None and normalized:
            p = (p[0] / 1000 * width, p[1] / 1000 * height)
        if p is not None:
            p = (p[0] * scale, p[1] * scale)
        return p

    return point


def _shrink(png: bytes, width: int, height: int, to_width: int) -> tuple[bytes, int, int, float]:
    import io

    from PIL import Image

    to_height = round(height * to_width / width)
    with Image.open(io.BytesIO(png)) as img:
        out = io.BytesIO()
        img.resize((to_width, to_height), Image.Resampling.LANCZOS).save(out, format="PNG")
    return out.getvalue(), to_width, to_height, width / to_width


def main(argv: list[str] | None = None) -> int:
    from anybrowser import open_engine
    from anybrowser.core.registry import models as model_registry

    from .__main__ import _options, _viewport_defaults

    parser = argparse.ArgumentParser(prog="python -m anybrowser_bench.grounding")
    parser.add_argument("--engine", default="chrome")
    parser.add_argument("-o", "--option", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--model", default="openai")
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--model-option", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--normalized", action="store_true", help="Ask for 0-1000, not pixels.")
    parser.add_argument("--seeds", type=int, default=2)
    parser.add_argument(
        "--image-width",
        type=int,
        nargs="+",
        default=[0],
        help="Shrink screenshots to these widths (0 = full size); one score each.",
    )
    args = parser.parse_args(argv)

    provider = model_registry.get(args.model)(**_options(args.model_option))

    async def go() -> int:
        options = {**_viewport_defaults(args.engine), **_options(args.option)}
        async with await open_engine(args.engine, **options) as engine:
            targets = await collect_targets(engine, seeds=range(args.seeds))
        for width in args.image_width:
            tokens: list[int] = []
            pointer = llm_pointer(
                provider,
                args.model_name,
                normalized=args.normalized,
                image_width=width or None,
                prompt_tokens=tokens,
            )
            report = await score(pointer, targets)
            size = f"{width}px" if width else "full"
            mean = sum(tokens) / len(tokens) if tokens else 0
            print(f"{args.model_name} @ {size}: {report.summary()}   {mean:.0f} prompt tokens")
            for miss in report.misses[:4]:
                print("  miss:", miss)
        return 0

    return asyncio.run(go())


if __name__ == "__main__":
    sys.exit(main())
