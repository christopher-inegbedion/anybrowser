"""``ModelProvider`` -- the interface an LLM backend implements.

The agent needs three things from a model and nothing else: send a multimodal
conversation, stream the reply, and report what it cost. Everything provider
specific -- role juggling, image encoding, tool-call schemas -- is the
provider's problem, because providers disagree about all of it in ways no
lowest-common-denominator wrapper survives.

Register your own in the ``relaykit.models`` entry-point group.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "Completion",
    "ImagePart",
    "Message",
    "ModelProvider",
    "Role",
    "Usage",
]


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class ImagePart:
    """One image in a message. ``data`` is raw bytes, never base64."""

    data: bytes
    media_type: str = "image/png"
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    text: str = ""
    images: Sequence[ImagePart] = ()


@dataclass(frozen=True, slots=True)
class Usage:
    """Tokens and money for one call.

    ``cost_usd`` is computed by the provider from its own price table, because
    only the provider knows whether a cache read was billed at a discount. A
    provider that genuinely cannot price a call reports ``0.0`` and says so in
    ``notes`` -- it must not guess.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cost_usd: float = 0.0
    notes: str = ""


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    stop_reason: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)


class ModelProvider(abc.ABC):
    """A source of completions."""

    #: Registry name. Must match the entry-point key.
    name: str = ""

    @abc.abstractmethod
    async def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stop: Sequence[str] = (),
        **options: Any,
    ) -> Completion: ...

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stop: Sequence[str] = (),
        **options: Any,
    ) -> AsyncIterator[str]:
        """Yield text deltas.

        Streaming is not a nicety here: it is what makes a running agent
        interruptible mid-decision. A provider without a streaming API should
        still implement this by yielding the finished text once, so callers need
        only one code path.
        """
        completion = await self.complete(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            stop=stop,
            **options,
        )
        yield completion.text

    def supports_images(self, model: str) -> bool:
        """Whether ``model`` accepts image parts.

        Getting this wrong is expensive and quiet: several text-only models
        answer an image-bearing request with a 4xx that reads like a transport
        error. Providers should answer from a real table, not a guess.
        """
        return True
