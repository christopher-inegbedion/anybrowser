"""The model-provider contract.

    pytest --pyargs relaykit_conformance --model openai --model-name gpt-4o-mini

Providers cost money to exercise, so this suite is opt-in and skips entirely
without ``--model``. Everything it asserts is cheap: short prompts, low token
caps. What it is really checking is the three things a provider gets wrong in
ways that surface much later -- streaming that does not stream, usage that is
silently zero, and an image-capable claim that is not true.
"""

from __future__ import annotations

import pytest

from relaykit.models.provider import ImagePart, Message, Role

# A 1x1 red PNG. Small enough to send to a vision model for a few tokens.
_PIXEL = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080200000090"
    "7753de0000000c4944415408d763f8cfc000000301010018dd8db00000000049454e44ae426082"
)


@pytest.fixture
def model_name(pytestconfig: pytest.Config) -> str:
    name = pytestconfig.getoption("--model-name")
    if not name:
        pytest.skip("no --model-name given")
    return str(name)


async def test_completes_a_prompt(provider, model_name):
    reply = await provider.complete(
        [Message(role=Role.USER, text="Reply with exactly the word: pong")],
        model=model_name,
        max_tokens=16,
    )
    assert reply.text.strip(), "provider returned empty text"
    assert "pong" in reply.text.lower()
    assert reply.model, "completion did not report which model answered"


async def test_reports_token_usage(provider, model_name):
    """Zero tokens for a real call means something upstream is metering nothing."""
    reply = await provider.complete(
        [Message(role=Role.USER, text="Say hi")], model=model_name, max_tokens=16
    )
    assert reply.usage.input_tokens > 0, "input tokens not reported"
    assert reply.usage.output_tokens > 0, "output tokens not reported"


async def test_prices_the_call_or_says_it_cannot(provider, model_name):
    """A provider may not know its price, but it must not invent one silently."""
    reply = await provider.complete(
        [Message(role=Role.USER, text="Say hi")], model=model_name, max_tokens=16
    )
    if reply.usage.cost_usd == 0.0:
        assert reply.usage.notes, (
            "cost_usd is 0.0 with no explanation; a provider that cannot price a "
            "call must say so in usage.notes rather than report a free call"
        )
    else:
        assert reply.usage.cost_usd > 0


async def test_system_message_is_honoured(provider, model_name):
    reply = await provider.complete(
        [
            Message(role=Role.SYSTEM, text="You always answer with a single digit."),
            Message(role=Role.USER, text="What is two plus two?"),
        ],
        model=model_name,
        max_tokens=16,
    )
    assert any(ch.isdigit() for ch in reply.text)


async def test_streaming_yields_before_it_finishes(provider, model_name):
    """Streaming is what makes a running agent interruptible mid-decision.

    A provider whose ``stream`` collects the whole completion and yields it once
    is *correct* but gives that up, so this only asserts that deltas arrive and
    reassemble -- not how many.
    """
    chunks: list[str] = []
    async for delta in provider.stream(
        [Message(role=Role.USER, text="Count: one two three")],
        model=model_name,
        max_tokens=32,
    ):
        chunks.append(delta)
    assert chunks, "stream yielded nothing"
    assert "".join(chunks).strip(), "stream yielded only empty deltas"


async def test_supports_images_is_answered_from_knowledge(provider, model_name):
    """Not a guess.

    Several text-only models answer an image-bearing request with a 4xx that
    reads exactly like a transport error, so a wrong answer here surfaces hours
    later as a mysterious intermittent failure.
    """
    claim = provider.supports_images(model_name)
    assert isinstance(claim, bool)
    if not claim:
        pytest.skip(f"{model_name} declares no image support")

    reply = await provider.complete(
        [
            Message(
                role=Role.USER,
                text="What colour is this image? One word.",
                images=[ImagePart(data=_PIXEL, media_type="image/png")],
            )
        ],
        model=model_name,
        max_tokens=16,
    )
    assert reply.text.strip(), "image-bearing request returned nothing"
