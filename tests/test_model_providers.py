"""Provider behaviour that needs no API key.

The model conformance suite needs credentials and costs money, so it is opt-in.
These cover the parts that are pure computation, and they are the parts that go
wrong quietly: pricing, role folding, and image encoding. A provider that
mis-prices a call is metering nothing; one that mis-folds a system message gets
a 400 that names nothing.
"""

from __future__ import annotations

import base64

import pytest

from relaykit.core.errors import ModelError
from relaykit.models import AnthropicProvider, OpenAICompatibleProvider
from relaykit.models.provider import ImagePart, Message, Role

_PIXEL = b"\x89PNG\r\n\x1a\n fake"


@pytest.fixture
def openai() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(api_key="test-key-not-real")


@pytest.fixture
def anthropic() -> AnthropicProvider:
    return AnthropicProvider(api_key="test-key-not-real")


# --------------------------------------------------------------------------- #
# Pricing                                                                      #
# --------------------------------------------------------------------------- #


def test_known_model_is_priced(anthropic):
    usage = anthropic._usage_counts(1_000_000, 1_000_000, 0, "claude-sonnet-5")
    assert usage.cost_usd == pytest.approx(3.00 + 15.00)
    assert not usage.notes


def test_unknown_model_reports_zero_with_an_explanation(anthropic):
    """Zero cost and silence is indistinguishable from a free call."""
    usage = anthropic._usage_counts(1000, 1000, 0, "some-model-we-never-heard-of")
    assert usage.cost_usd == 0.0
    assert "no price on file" in usage.notes
    assert usage.input_tokens == 1000, "token counts must survive an unknown price"


def test_cache_read_accounting_differs_by_provider():
    """The two APIs disagree about what `input_tokens` includes, and it is easy
    to miss.

    Anthropic reports cache reads *alongside* input_tokens, so they are added.
    OpenAI reports them *inside* prompt_tokens, so they are subtracted before
    being re-priced. Using either rule for the other provider misbills every
    cached request, silently and in opposite directions.
    """
    anthropic = AnthropicProvider(api_key="k")
    openai = OpenAICompatibleProvider(api_key="k")

    # Anthropic: 1M fresh + 1M cached = 2M tokens billed, the cached at a discount.
    a = anthropic._usage_counts(1_000_000, 0, 1_000_000, "claude-sonnet-5")
    assert a.cost_usd == pytest.approx(3.00 + 0.30)

    # OpenAI: 1M total of which 1M was cached = nothing at the full rate.
    o = openai._usage_counts(1_000_000, 0, 1_000_000, "gpt-4o")
    assert o.cost_usd == pytest.approx(1.25)

    # Either way a cache read costs less than the same tokens fresh.
    assert o.cost_usd < openai._usage_counts(1_000_000, 0, 0, "gpt-4o").cost_usd
    assert (
        anthropic._usage_counts(0, 0, 1_000_000, "claude-sonnet-5").cost_usd
        < anthropic._usage_counts(1_000_000, 0, 0, "claude-sonnet-5").cost_usd
    )


def test_openai_prices_cached_input_from_its_own_table(openai):
    full = openai._usage_counts(1_000_000, 0, 0, "gpt-4o")
    cached = openai._usage_counts(1_000_000, 0, 1_000_000, "gpt-4o")
    assert cached.cost_usd == pytest.approx(1.25)
    assert full.cost_usd == pytest.approx(2.50)


# --------------------------------------------------------------------------- #
# Image support                                                                #
# --------------------------------------------------------------------------- #


def test_image_support_is_answered_from_a_table(openai, anthropic):
    assert openai.supports_images("gpt-4o") is True
    assert anthropic.supports_images("claude-sonnet-5") is True


def test_unknown_models_fail_closed_on_images(openai):
    """Safer direction: a text-only model rejects images with an opaque 4xx."""
    assert openai.supports_images("some-random-text-model") is False


# --------------------------------------------------------------------------- #
# Role handling                                                                #
# --------------------------------------------------------------------------- #


def test_anthropic_lifts_system_out_of_the_messages(anthropic):
    """Anthropic takes `system` as a top-level parameter, not a message."""
    payload = anthropic._payload(
        [
            Message(role=Role.SYSTEM, text="be terse"),
            Message(role=Role.USER, text="hello"),
        ],
        model="claude-sonnet-5",
        temperature=0.0,
        max_tokens=64,
        stop=(),
        options={},
    )
    assert "be terse" in str(payload.get("system"))
    assert all(m["role"] != "system" for m in payload["messages"])
    assert payload["messages"][0]["role"] == "user"


def test_openai_keeps_system_in_the_messages(openai):
    payload = openai._payload(
        [
            Message(role=Role.SYSTEM, text="be terse"),
            Message(role=Role.USER, text="hello"),
        ],
        model="gpt-4o",
        temperature=0.0,
        max_tokens=64,
        stop=(),
        options={},
    )
    assert payload["messages"][0]["role"] == "system"


# --------------------------------------------------------------------------- #
# Images on the wire                                                           #
# --------------------------------------------------------------------------- #


def test_anthropic_encodes_images_as_base64_source(anthropic):
    payload = anthropic._payload(
        [Message(role=Role.USER, text="what is this", images=[ImagePart(data=_PIXEL)])],
        model="claude-sonnet-5",
        temperature=0.0,
        max_tokens=64,
        stop=(),
        options={},
    )
    parts = payload["messages"][0]["content"]
    image = next(p for p in parts if p.get("type") == "image")
    assert image["source"]["data"] == base64.b64encode(_PIXEL).decode()
    assert image["source"]["media_type"] == "image/png"


def test_openai_encodes_images_as_a_data_url(openai):
    payload = openai._payload(
        [Message(role=Role.USER, text="what is this", images=[ImagePart(data=_PIXEL)])],
        model="gpt-4o",
        temperature=0.0,
        max_tokens=64,
        stop=(),
        options={},
    )
    parts = payload["messages"][0]["content"]
    image = next(p for p in parts if p.get("type") == "image_url")
    expected = base64.b64encode(_PIXEL).decode()
    assert image["image_url"]["url"] == f"data:image/png;base64,{expected}"


# --------------------------------------------------------------------------- #
# Credentials                                                                  #
# --------------------------------------------------------------------------- #


def test_missing_key_is_a_clear_error_not_a_401(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ModelError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider()


def test_the_key_never_appears_in_an_error(monkeypatch):
    """A key in an exception message ends up in logs and bug reports."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-super-secret-value")
    provider = AnthropicProvider()
    usage = provider._usage_counts(1, 1, 0, "unknown-model")
    assert "sk-super-secret" not in usage.notes
    assert "sk-super-secret" not in repr(provider)


def test_openai_price_tables_stay_in_step():
    """Every priced model needs a cached rate, or it is billed at full rate.

    The fallback is safe rather than fatal, but a silent overcharge is still
    wrong; this fails when someone adds a model to one table only.
    """
    from relaykit.models.openai_compatible import (
        _OPENAI_CACHED_INPUT_PRICES,
        OPENAI_PRICES,
    )

    missing = set(OPENAI_PRICES) - set(_OPENAI_CACHED_INPUT_PRICES)
    assert not missing, f"priced models with no cached rate: {sorted(missing)}"
