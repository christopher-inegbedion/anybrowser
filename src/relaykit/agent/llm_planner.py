"""A planner that asks a model what to do next.

The default planner. It renders the goal, the page, the history and the tool
schemas into one prompt, and parses a single JSON decision back.

Two choices here are deliberate and worth defending.

**JSON in the response, not provider tool-calling.** Tool-call formats differ
per provider in ways no wrapper survives intact, and several models RelayKit
should support have none at all. Asking for one JSON object works everywhere and
keeps :class:`~relaykit.models.provider.ModelProvider` small.

**`narrative` is required.** A model that emits no reasoning tokens still has to
say why. A history reading "clicked, clicked, clicked" cannot be debugged by
anyone -- including the model itself on the next step.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from ..core.errors import ModelError
from ..models.provider import ImagePart, Message, ModelProvider, Role
from .planner import Decision, Observation, Planner
from .runner import render_history
from .tool import Tool

__all__ = ["SYSTEM_PROMPT", "LLMPlanner"]

SYSTEM_PROMPT = """\
You are driving a web browser to accomplish a goal. You see the page as a list \
of interactive elements, each with a handle, plus a screenshot when one is \
available.

Choose exactly ONE action per turn. Reply with one JSON object and nothing else:

{"tool": "<tool name>", "arguments": {...}, "narrative": "<why, in one sentence>"}

For example, to click the element listed as `handle=3:12`:

{"tool": "click", "arguments": {"handle": "3:12"}, "narrative": "this is the login button"}

When the goal is achieved, reply instead with:

{"done": true, "answer": "<what you found, or what you did>", "narrative": "<why you are finished>"}

Rules that matter:

- Every element below is listed as `handle=<id>`. Pass that id EXACTLY, copied \
character for character. It is an opaque id such as "3:12" -- never the label, \
the tag, or anything you read off the screenshot.
- Act only on handles in the CURRENT listing. Handles from earlier turns are \
stale and will be refused.
- A result marked [no effect] means the action changed nothing. Doing it again \
will change nothing again. Try something else: scroll, look, or pick a \
different element.
- A result marked [failed] means the action could not run at all. Read the \
reason before choosing.
- "narrative" is required, always, and must say why you chose this action -- \
not what the action is.
- Do not claim the goal is done without evidence in the page that it is.\
"""


class LLMPlanner(Planner):
    """Ask a model for the next action."""

    name = "llm"

    def __init__(
        self,
        provider: ModelProvider,
        *,
        model: str,
        max_elements: int = 60,
        temperature: float = 0.0,
        max_tokens: int = 800,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        self._provider = provider
        self._model = model
        self._max_elements = max_elements
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._system_prompt = system_prompt
        #: Accumulated across a run, so a caller can meter what an agent cost.
        self.total_cost_usd = 0.0
        self.total_tokens = 0

    # ------------------------------------------------------------------ #
    # Prompting                                                           #
    # ------------------------------------------------------------------ #

    def _render_page(self, observation: Observation) -> str:
        page = observation.snapshot
        lines = [f"URL: {page.url}", f"Title: {page.title}", "", "Elements:"]
        if not page.elements:
            lines.append("  (none found — try scrolling, or the page may still be loading)")
        for element in page.elements[: self._max_elements]:
            # `description` already starts with the tag or role, so printing
            # both made the line read as `[2:0] a 'a Learn more'` -- and a model
            # will reach for the quoted text as the identifier. Give the handle
            # its own labelled column and print the label alone.
            kind = element.role or element.tag
            label = element.label or element.placeholder or element.value or "(no label)"
            detail = f"handle={element.handle}  <{kind}>  {label!r}"
            if element.value and element.value != label:
                detail += f"  value={element.value!r}"
            if element.disabled:
                detail += "  (disabled)"
            lines.append("  " + detail)
        if len(page.elements) > self._max_elements:
            hidden = len(page.elements) - self._max_elements
            lines.append(f"  … and {hidden} more not shown")
        return "\n".join(lines)

    def _render_tools(self, tools: Sequence[Tool]) -> str:
        return "\n".join(
            f"- {tool.name}: {tool.description}\n  arguments: "
            f"{json.dumps(tool.parameters.get('properties', {}))}"
            for tool in tools
        )

    def _build_messages(self, observation: Observation, tools: Sequence[Tool]) -> list[Message]:
        parts = [
            f"GOAL: {observation.goal}",
            "",
            "TOOLS:",
            self._render_tools(tools),
            "",
            "PAGE:",
            self._render_page(observation),
        ]
        if observation.history:
            parts += ["", "WHAT YOU HAVE DONE:", render_history(observation.history)]
        if observation.notes:
            parts += ["", "NOTES:", *(f"- {n}" for n in observation.notes)]
        parts += ["", f"Step {observation.step_index + 1}. Choose one action."]

        images: list[ImagePart] = []
        if observation.screenshot is not None and self._provider.supports_images(self._model):
            images.append(
                ImagePart(
                    data=observation.screenshot.data,
                    media_type=f"image/{observation.screenshot.format}",
                )
            )
        return [
            Message(role=Role.SYSTEM, text=self._system_prompt),
            Message(role=Role.USER, text="\n".join(parts), images=images),
        ]

    # ------------------------------------------------------------------ #
    # Deciding                                                            #
    # ------------------------------------------------------------------ #

    async def decide(self, observation: Observation, tools: Sequence[Tool]) -> Decision:
        messages = self._build_messages(observation, tools)
        completion = await self._provider.complete(
            messages,
            model=self._model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        )
        self.total_cost_usd += completion.usage.cost_usd
        self.total_tokens += completion.usage.input_tokens + completion.usage.output_tokens
        return self._parse(completion.text, tools)

    def _parse(self, text: str, tools: Sequence[Tool]) -> Decision:
        payload = _extract_json(text)
        if payload is None:
            raise ModelError(f"planner did not return JSON: {text[:300]!r}")

        narrative = str(payload.get("narrative") or "").strip()
        if payload.get("done"):
            return Decision(
                tool="",
                arguments={},
                # A model that finishes without saying why still has to say
                # something; refusing here would fail a successful run over
                # a missing sentence.
                narrative=narrative or "the goal appears to be met",
                done=True,
                answer=str(payload.get("answer") or ""),
            )

        tool = str(payload.get("tool") or "").strip()
        if not tool:
            raise ModelError(f"planner named no tool: {text[:300]!r}")
        known = {t.name for t in tools}
        if tool not in known:
            raise ModelError(f"planner chose unknown tool {tool!r}; available: {sorted(known)}")

        arguments = payload.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        return Decision(
            tool=tool,
            arguments=arguments,
            narrative=narrative or f"calling {tool}",
        )


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _extract_json(text: str) -> dict[str, Any] | None:
    """Find the JSON object in a model's reply.

    Models wrap JSON in prose and fences no matter how firmly the prompt asks
    them not to. Being lenient here costs nothing; being strict costs a run.
    """
    candidates: list[str] = []
    fenced = _FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1))
    candidates.append(text)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None
