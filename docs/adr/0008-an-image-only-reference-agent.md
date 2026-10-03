# ADR-0008: An image-only reference agent, in screenshot pixels

**Status:** Accepted · **Date:** 2026-10-03

## Context

The reference agent reads the page through the DOM: a list of elements with
handles, plus the page text. That cannot work on a page the DOM does not
describe. A canvas game, for example, is one element with no text, and its
buttons exist only as pixels. The engine interface has always accepted
coordinates: `click`, `hover` and `type_text` take a `Point`, and
`core/types.py` defines a normalized 0–1000 space "because models emit it".
Nothing in `agent/` ever produced one. The project this one grew out of drove
clicks from a vision model's coordinates, and that path was not ported.

There is now a concrete use for it: an agent that decides from the screenshot
alone, using one small local model for both seeing and acting, and that gets
better through fine-tuning. Whether that is possible depends on whether a small
model can point accurately. We measured it rather than assumed it: 32–34 named
targets on the bench's pages, a hit meaning the point lands inside the real box,
on an M4 with 16 GB.

| model | coordinates | hits | per call |
|---|---|---|---|
| qwen2.5vl:3b | pixels | 30/32 (canvas 8/8) | ~6s on a new image |
| qwen2.5vl:3b | 0–1000 | 0/32 | |
| qwen3-vl:4b | 0–1000 | 27/32 | 27s (reasons even with think=false) |
| gemma3:4b | either | ≤2/32 | |

## Decision

**A second reference agent, `VisionPlanner` with `vision_tools()`, alongside the
DOM one rather than replacing it.** The model is shown the goal, its own history
and one screenshot. It gets no element list, no page text, no URL and no title.
A test checks that none of these leak into the prompt, because a leaked label
would let the agent pass tasks meant to test vision.

**Coordinates are screenshot pixels, not the normalized space.** The one small
model that could point was trained on pixels and fails completely on 0–1000.
The normalized space stays in `core/types.py` for models that do use it. The
reference agent follows the measurement.

**The screenshot is always shown at the viewport's CSS size.** This is what
lets a pixel in the image be a viewport point with no conversion in any tool. On
a 2x display the capture is resized down before the model sees it. The contract
lives in one place, the planner, instead of a scale factor that every tool would
have to apply.

**Image size is part of the contract, not a detail.** At headless Chrome's
default 756x469, qwen2.5vl's guesses all came out about 1.5x too large, which
looks like Ollama rescaling small images before the model sees them. At 1280x800
they were correct. The Chrome engine gained `viewport_width` and
`viewport_height`, and a browser it launches defaults to 1280x800, the same as
Playwright. The bench asks for that size explicitly, because the engine leaves a
browser it did not launch at whatever size it already had. That is how a stale
Chrome produced a run with every click off by 1.5x and nothing in the report to
say so.

**Canvases are fingerprinted, coarsely.** The page signature behind `changed`
was text, URL and focus, by design, so that a caret blink would not count as
progress. A canvas is only pixels, so a click that redrew a canvas menu came
back "hit nothing", and the vision agent's history marked the right action
[no effect]. The largest two canvases are now shrunk to 48x30 at four bits per
channel and hashed into the signature. Two conformance tests cover both sides:
a redraw must count as a change, and a still canvas must not.

## Consequences

Good: pages that only exist as pixels are reachable. The bench has two canvas
tasks that a DOM agent cannot pass, and a grounding check
(`python -m anybrowser_bench.grounding`) that measures aim directly. That is the
cheapest number to watch while fine-tuning.

Bad: a canvas that animates continuously reads as always changing. `await_change`
returns at once on it, and the stuck breaker cannot catch futile clicks there.
On such a page that is the true answer to "did anything change", but it is less
useful than the answer on a still page.

Also bad: a fresh screenshot costs about 6s of prompt processing on the target
hardware, about 1,400 image tokens at 1280x800. The first grounding probe
reported 0.6s per call because it asked several questions about the same
screenshot and Ollama cached it. An agent never sees the same screenshot twice,
so about 6s per step is the floor until the image gets cheaper.

## When we would revisit

If a model that is better at pointing works in normalized coordinates, the tools
take a coordinate space as a parameter instead of assuming pixels. If image cost
dominates training throughput, smaller screenshots are worth trying, but only
with the grounding check run at that size, since size is where this broke the
first time.
