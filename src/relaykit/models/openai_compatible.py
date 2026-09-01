"""OpenAI's wire format without an OpenAI SDK dependency.

Many model hosts implement the Chat Completions protocol, so this provider uses
the small HTTP surface directly and keeps ``httpx`` optional.  Request shaping,
SSE parsing, and metering live here because those are precisely the details
that differ between otherwise compatible endpoints.
"""

from __future__ import annotations

import base64
import importlib
import json
import os
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from typing import Any

from ..core.errors import ModelError
from .provider import Completion, Message, ModelProvider, Usage

__all__ = ["OPENAI_PRICES", "OpenAICompatibleProvider"]

# USD per million tokens: (uncached input, output).  Cached-input rates are
# separate because the discount is not uniform across model generations.
OPENAI_PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "o3": (2.00, 8.00),
    "o4-mini": (1.10, 4.40),
}

_OPENAI_CACHED_INPUT_PRICES: dict[str, float] = {
    "gpt-4o": 1.25,
    "gpt-4o-mini": 0.075,
    "gpt-4.1": 0.50,
    "gpt-4.1-mini": 0.10,
    "o3": 0.50,
    "o4-mini": 0.275,
}

# The named reasoning models accept images, but older o-series models and many
# third-party text models do not.  Unknown names therefore fail closed.
# Listed independently of the price table: a price and a vision capability are
# unrelated facts, and deriving one from the other means adding a price
# silently claims image support the model may not have.
_IMAGE_MODELS = frozenset({"gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "o3", "o4-mini"})
_IMAGE_MODEL_PREFIXES = ("gpt-4o-", "gpt-4.1-", "o3-", "o4-mini-")


class OpenAICompatibleProvider(ModelProvider):
    """Call OpenAI or another host implementing Chat Completions."""

    name = "openai"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 60.0,
        image_models: Iterable[str] | None = None,
    ) -> None:
        """
        ``image_models`` extends the built-in vision table.

        It exists because this provider is pointed at gateways -- OpenRouter,
        Together, vLLM, Ollama -- whose model names cannot be known here. The
        table fails closed for anything unrecognised, which is the safe
        direction (a text-only model rejects an image with an opaque 4xx), but
        it also means a perfectly capable gateway model silently never receives
        screenshots. Name them here and they will.
        """
        try:
            httpx = importlib.import_module("httpx")
        except ImportError as exc:
            raise ModelError(
                "httpx is required for the OpenAI-compatible model provider; "
                "pip install 'relaykit[agent]'"
            ) from exc

        resolved_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not resolved_key:
            raise ModelError("OpenAI API key is required; pass api_key= or set OPENAI_API_KEY")
        self._httpx: Any = httpx
        self._api_key = resolved_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._image_models = frozenset(image_models or ())
        self.last_usage = Usage()

    def supports_images(self, model: str) -> bool:
        if model in self._image_models:
            return True
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
                    f"{self._base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                )
                response.raise_for_status()
                data: Any = response.json()
        except self._httpx.HTTPStatusError as exc:
            raise ModelError(
                "OpenAI-compatible API returned an HTTP error",
                status_code=exc.response.status_code,
            ) from exc
        except self._httpx.HTTPError as exc:
            raise ModelError("OpenAI-compatible API request failed") from exc
        except (TypeError, ValueError) as exc:
            raise ModelError("OpenAI-compatible API returned invalid JSON") from exc

        try:
            raw = self._mapping(data)
            choice = self._mapping(self._sequence(raw["choices"])[0])
            message = self._mapping(choice["message"])
            answered_model = str(raw.get("model") or model)
            usage = self._usage(self._mapping(raw.get("usage", {})), model)
            completion = Completion(
                text=self._text(message.get("content", "")),
                usage=usage,
                model=answered_model,
                stop_reason=str(choice.get("finish_reason") or ""),
                raw=raw,
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ModelError("OpenAI-compatible API returned an invalid response") from exc
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
        payload["stream_options"] = {"include_usage": True}
        self.last_usage = self._usage({}, model)
        try:
            async with (
                self._httpx.AsyncClient(timeout=self._timeout) as client,
                client.stream(
                    "POST",
                    f"{self._base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                ) as response,
            ):
                response.raise_for_status()
                async for event_data in self._sse_data(response.aiter_lines()):
                    if event_data == "[DONE]":
                        break
                    try:
                        event = self._mapping(json.loads(event_data))
                        if event.get("error") is not None:
                            raise ModelError("OpenAI-compatible API returned a stream error")
                        usage_data = event.get("usage")
                        if usage_data is not None:
                            self.last_usage = self._usage(self._mapping(usage_data), model)
                        choices = self._sequence(event.get("choices", []))
                        if choices:
                            delta = self._mapping(self._mapping(choices[0]).get("delta", {}))
                            text = self._text(delta.get("content", ""))
                            if text:
                                yield text
                    except (json.JSONDecodeError, TypeError, ValueError) as exc:
                        raise ModelError(
                            "OpenAI-compatible API returned an invalid stream event"
                        ) from exc
        except self._httpx.HTTPStatusError as exc:
            raise ModelError(
                "OpenAI-compatible API returned an HTTP error",
                status_code=exc.response.status_code,
            ) from exc
        except self._httpx.HTTPError as exc:
            raise ModelError("OpenAI-compatible API request failed") from exc

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
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
        payload: dict[str, Any] = {
            "model": model,
            "messages": [self._message(message) for message in messages],
        }
        # OpenAI reasoning models reject both legacy token limits and sampling
        # temperature, although they share the Chat Completions endpoint.
        if model == "o3" or model == "o4-mini" or model.startswith(("o3-", "o4-mini-")):
            payload["max_completion_tokens"] = max_tokens
        else:
            payload["temperature"] = temperature
            payload["max_tokens"] = max_tokens
        if stop:
            payload["stop"] = list(stop)
        payload.update(options)
        return payload

    @staticmethod
    def _message(message: Message) -> dict[str, Any]:
        if not message.images:
            return {"role": message.role.value, "content": message.text}
        content: list[dict[str, Any]] = []
        if message.text:
            content.append({"type": "text", "text": message.text})
        for image in message.images:
            encoded = base64.b64encode(image.data).decode("ascii")
            part: dict[str, Any] = {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{image.media_type};base64,{encoded}",
                },
            }
            if image.detail:
                part["image_url"]["detail"] = image.detail
            content.append(part)
        return {"role": message.role.value, "content": content}

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
        details = OpenAICompatibleProvider._mapping(data.get("prompt_tokens_details", {}))
        return OpenAICompatibleProvider._usage_counts(
            int(data.get("prompt_tokens") or 0),
            int(data.get("completion_tokens") or 0),
            int(details.get("cached_tokens") or 0),
            model,
        )

    @staticmethod
    def _usage_counts(
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
        model: str,
    ) -> Usage:
        prices = OPENAI_PRICES.get(model)
        if prices is None:
            return Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_input_tokens=cached_tokens,
                notes=f"no price on file for {model}",
            )
        input_price, output_price = prices
        # Two tables that must agree. Subscripting the second would turn a
        # missing entry into a KeyError mid-call; falling back to the uncached
        # rate merely overstates the cost slightly, which is the safe direction
        # for something being metered.
        cached_price = _OPENAI_CACHED_INPUT_PRICES.get(model, input_price)
        uncached_tokens = max(0, input_tokens - cached_tokens)
        cost = (
            uncached_tokens * input_price
            + cached_tokens * cached_price
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
    def _text(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, Sequence):
            parts: list[str] = []
            for part in value:
                if isinstance(part, Mapping) and part.get("type") == "text":
                    text = part.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "".join(parts)
        return ""
