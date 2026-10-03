"""Keep exactly what the model saw and said, step by step, for training on.

A trace says what an agent did. Training needs what it was *shown*: the system
prompt, the user message, the screenshot bytes, and the request options (the
schema, under ADR-0009), each paired with the reply. Fine-tuning on a
reconstruction of those inputs teaches the model a prompt it will never see.

So recording wraps the provider and keeps every exchange verbatim. An attempt
is written to disk with its judge outcome, successes and failures alike:
filtering is a decision for whoever builds a dataset, and failures are the
material for methods that learn from contrast.

Layout, one directory per attempt::

    <dir>/<task>-s<seed>-<n>/attempt.json      task, seed, outcome, score, answer
    <dir>/<task>-s<seed>-<n>/step-00.json      messages, options, reply, step outcome
    <dir>/<task>-s<seed>-<n>/step-00-0.png     each image the step's messages carried
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from anybrowser.agent import RunResult
from anybrowser.models.provider import Completion, Message, ModelProvider

__all__ = ["Exchange", "RecordedRun", "RecordingProvider", "SeedingProvider", "write_attempt"]


@dataclass(frozen=True, slots=True)
class Exchange:
    messages: tuple[Message, ...]
    options: Mapping[str, Any]
    reply: str
    stop_reason: str = ""


@dataclass(slots=True)
class RecordedRun(RunResult):
    """A RunResult that also carries the model exchanges behind it."""

    exchanges: list[Exchange] = field(default_factory=list)


class RecordingProvider(ModelProvider):
    """Delegates everything to ``inner`` and keeps each completion verbatim."""

    def __init__(self, inner: ModelProvider) -> None:
        self._inner = inner
        self.name = inner.name
        self.exchanges: list[Exchange] = []

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stop: Sequence[str] = (),
        **options: Any,
    ) -> Completion:
        completion = await self._inner.complete(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            stop=stop,
            **options,
        )
        self.exchanges.append(
            Exchange(tuple(messages), dict(options), completion.text, completion.stop_reason)
        )
        return completion

    def structured_output(self, schema: Mapping[str, Any]) -> Mapping[str, Any] | None:
        return self._inner.structured_output(schema)

    def supports_images(self, model: str) -> bool:
        return self._inner.supports_images(model)


class SeedingProvider(ModelProvider):
    """Sends a different ``seed`` with every request.

    mlx-vlm's server samples from keys derived from a fixed default seed, so
    at temperature 0.7 three repeats of one state came back identical, byte
    for byte -- and the first self-improvement run's "sampled" collection was
    in fact deterministic. ``seed`` is a standard OpenAI parameter; a server
    that does not take it ignores it, and one that already varies (Ollama)
    simply varies with it.
    """

    def __init__(self, inner: ModelProvider, start: int = 0) -> None:
        self._inner = inner
        self.name = inner.name
        self._seeds = itertools.count(start)

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stop: Sequence[str] = (),
        **options: Any,
    ) -> Completion:
        options.setdefault("seed", next(self._seeds))
        return await self._inner.complete(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            stop=stop,
            **options,
        )

    def structured_output(self, schema: Mapping[str, Any]) -> Mapping[str, Any] | None:
        return self._inner.structured_output(schema)

    def supports_images(self, model: str) -> bool:
        return self._inner.supports_images(model)


_ids = itertools.count()


def write_attempt(root: Path, attempt: Any, exchanges: Sequence[Exchange]) -> Path:
    """Write one attempt's exchanges and outcome. Returns its directory."""
    where = root / f"{attempt.task_id}-s{attempt.seed}-{next(_ids):04d}"
    where.mkdir(parents=True, exist_ok=False)
    summary = asdict(attempt)
    (where / "attempt.json").write_text(json.dumps(summary, indent=2))
    for index, exchange in enumerate(exchanges):
        messages = []
        for message in exchange.messages:
            images: list[str] = []
            for image in message.images:
                name = f"step-{index:02d}-{len(images)}.png"
                (where / name).write_bytes(image.data)
                images.append(name)
            messages.append({"role": message.role.value, "text": message.text, "images": images})
        step = attempt.trace[index] if index < len(attempt.trace) else None
        (where / f"step-{index:02d}.json").write_text(
            json.dumps(
                {
                    "messages": messages,
                    "options": exchange.options,
                    "reply": exchange.reply,
                    "stop_reason": exchange.stop_reason,
                    # What the engine said this step did; None when the reply
                    # never became an action (unparseable, or the run ended).
                    "step": step,
                },
                indent=2,
            )
        )
    return where
