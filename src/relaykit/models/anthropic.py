"""Anthropic Messages API adaptation for RelayKit's neutral conversation model.

Anthropic's top-level system prompt, alternating roles, content-block images,
and event-oriented usage accounting cannot be represented faithfully by a
generic OpenAI adapter.  Keeping that translation here makes the public model
contract simple while preserving Anthropic's real streaming behaviour.
"""

from __future__ import annotations

import base64
import importlib
import json
import os
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from typing import Any

from ..core.errors import ModelError
from .provider import Completion, Message, ModelProvider, Role, Usage

__all__ = ["ANTHROPIC_PRICES", "AnthropicProvider"]

# USD per million tokens: (uncached input, output).  Anthropic cache reads are
# billed at 10% of the ordinary input rate for these models.
ANTHROPIC_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-fable-5": (10.00, 50.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
}

_CACHE_READ_FACTOR = 0.10
# Listed independently of the price table: a price and a vision capability are
# unrelated facts. Every current Claude model accepts images, but deriving that
# from the price table would make a future text-only entry claim otherwise.
_IMAGE_MODELS = frozenset(
    {"claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-haiku-4-5-20251001"}
)
_IMAGE_MODEL_PREFIXES = (
    "claude-opus-5-",
    "claude-sonnet-5-",
    "claude-fable-5-",
    "claude-haiku-4-5-",
)


class AnthropicProvider(ModelProvider):
    """Call Anthropic's Messages API."""

    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.anthropic.com/v1",
        timeout: float = 60.0,
    ) -> None:
        try:
            httpx = importlib.import_module("httpx")
        except ImportError as exc:
            raise ModelError(
                "httpx is required for the Anthropic model provider; pip install 'relaykit[agent]'"
            ) from exc

        resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not resolved_key:
            raise ModelError(
                "Anthropic API key is required; pass api_key= or set ANTHROPIC_API_KEY"
            )
        self._httpx: Any = httpx
        self._api_key = resolved_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self.last_usage = Usage()

    def supports_images(self, model: str) -> bool:
        return model in _IMAGE_MODELS or model.startswith(_IMAGE_MODEL_PREFIXES)

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
        payload = self._payload(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            stop=stop,
            options=options,
        )
        try:
            async with self._httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/messages",
                    headers=self._headers(),
                    json=payload,
                )
                response.raise_for_status()
                data: Any = response.json()
        except self._httpx.HTTPStatusError as exc:
            raise ModelError(
                "Anthropic API returned an HTTP error",
                status_code=exc.response.status_code,
            ) from exc
        except self._httpx.HTTPError as exc:
            raise ModelError("Anthropic API request failed") from exc
        except (TypeError, ValueError) as exc:
            raise ModelError("Anthropic API returned invalid JSON") from exc

        try:
            raw = self._mapping(data)
            usage = self._usage(self._mapping(raw.get("usage", {})), model)
            content = self._sequence(raw.get("content", []))
            completion = Completion(
                text=self._content_text(content),
                usage=usage,
                model=str(raw.get("model") or model),
                stop_reason=str(raw.get("stop_reason") or ""),
                raw=raw,
            )
        except (TypeError, ValueError) as exc:
            raise ModelError("Anthropic API returned an invalid response") from exc
        self.last_usage = completion.usage
        return completion

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
        payload = self._payload(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            stop=stop,
            options=options,
        )
        payload["stream"] = True
        input_tokens = 0
        output_tokens = 0
        cached_tokens = 0
        self.last_usage = self._usage({}, model)
        try:
            async with (
                self._httpx.AsyncClient(timeout=self._timeout) as client,
                client.stream(
                    "POST",
                    f"{self._base_url}/messages",
                    headers=self._headers(),
                    json=payload,
                ) as response,
            ):
                response.raise_for_status()
                async for event_data in self._sse_data(response.aiter_lines()):
                    try:
                        event = self._mapping(json.loads(event_data))
                        event_type = event.get("type")
                        if event_type == "error":
                            raise ModelError("Anthropic API returned a stream error")
                        usage_data = event.get("usage")
                        if event_type == "message_start":
                            message = self._mapping(event.get("message", {}))
                            usage_data = message.get("usage")
                        if usage_data is not None:
                            usage = self._mapping(usage_data)
                            input_tokens = int(usage.get("input_tokens") or input_tokens)
                            output_tokens = int(usage.get("output_tokens") or output_tokens)
                            cached_tokens = int(
                                usage.get("cache_read_input_tokens") or cached_tokens
                            )
                            self.last_usage = self._usage_counts(
                                input_tokens,
                                output_tokens,
                                cached_tokens,
                                model,
                            )
                        if event_type == "content_block_delta":
                            delta = self._mapping(event.get("delta", {}))
                            text = delta.get("text")
                            if delta.get("type") == "text_delta" and isinstance(text, str):
                                yield text
                    except (json.JSONDecodeError, TypeError, ValueError) as exc:
                        raise ModelError("Anthropic API returned an invalid stream event") from exc
        except self._httpx.HTTPStatusError as exc:
            raise ModelError(
                "Anthropic API returned an HTTP error",
                status_code=exc.response.status_code,
            ) from exc
        except self._httpx.HTTPError as exc:
            raise ModelError("Anthropic API request failed") from exc

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    def _payload(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float,
        max_tokens: int,
        stop: Sequence[str],
        options: Mapping[str, Any],
    ) -> dict[str, Any]:
        system = "\n\n".join(
            message.text for message in messages if message.role is Role.SYSTEM and message.text
        )
        conversation = self._alternating_messages(
            message for message in messages if message.role is not Role.SYSTEM
        )
        payload: dict[str, Any] = {
            "model": model,
            "messages": conversation,
            "max_tokens": max_tokens,
        }
        # Claude 5's adaptive thinking rejects sampling controls. Haiku 4.5
        # retains the traditional temperature parameter.
        if model == "claude-haiku-4-5-20251001" or model.startswith("claude-haiku-4-5-"):
            payload["temperature"] = temperature
        if system:
            payload["system"] = system
        if stop:
            payload["stop_sequences"] = list(stop)
        payload.update(options)
        return payload

    @classmethod
    def _alternating_messages(cls, messages: Iterable[Message]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for message in messages:
            content = cls._content(message)
            role = message.role.value
            if result and result[-1]["role"] == role:
                result[-1]["content"].extend(content)
            else:
                result.append({"role": role, "content": content})
        return result

    @staticmethod
    def _content(message: Message) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = []
        if message.text:
            content.append({"type": "text", "text": message.text})
        for image in message.images:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image.media_type,
                        "data": base64.b64encode(image.data).decode("ascii"),
                    },
                }
            )
        return content

    @staticmethod
    async def _sse_data(lines: AsyncIterator[str]) -> AsyncIterator[str]:
        data_lines: list[str] = []
        async for line in lines:
            if not line:
                if data_lines:
                    yield "\n".join(data_lines)
                    data_lines.clear()
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if data_lines:
            yield "\n".join(data_lines)

    @staticmethod
    def _usage(data: Mapping[str, Any], model: str) -> Usage:
        return AnthropicProvider._usage_counts(
            int(data.get("input_tokens") or 0),
            int(data.get("output_tokens") or 0),
            int(data.get("cache_read_input_tokens") or 0),
            model,
        )

    @staticmethod
    def _usage_counts(
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
        model: str,
    ) -> Usage:
        prices = ANTHROPIC_PRICES.get(model)
        if prices is None:
            return Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_input_tokens=cached_tokens,
                notes=f"no price on file for {model}",
            )
        input_price, output_price = prices
        # Anthropic reports cache reads alongside, rather than inside,
        # ``input_tokens``; subtracting them would underbill cached requests.
        cost = (
            input_tokens * input_price
            + cached_tokens * input_price * _CACHE_READ_FACTOR
            + output_tokens * output_price
        ) / 1_000_000
        return Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_tokens,
            cost_usd=cost,
        )

    @staticmethod
    def _mapping(value: Any) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise TypeError("expected an object")
        return value

    @staticmethod
    def _sequence(value: Any) -> Sequence[Any]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise TypeError("expected an array")
        return value

    @staticmethod
    def _content_text(content: Sequence[Any]) -> str:
        parts: list[str] = []
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
