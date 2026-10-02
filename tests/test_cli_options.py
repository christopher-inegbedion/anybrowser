"""CLI option parsing, and that model options actually reach the provider.

`anybrowser run` could not be pointed at a gateway before `--model-option`
existed: the OpenAI-compatible provider is built for OpenRouter, vLLM and Ollama,
but the CLI had no way to pass it a `base_url`, so it could only ever reach the
default endpoint.
"""

from __future__ import annotations

import argparse

import pytest

from anybrowser.cli.main import _parse_options, build_parser, cmd_run


def test_model_options_parse_into_constructor_kwargs():
    parsed = _parse_options(
        ["base_url=https://openrouter.ai/api/v1", "api_key=sk-or-v1-abc"],
        "--model-option",
    )
    assert parsed == {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "sk-or-v1-abc",
    }


def test_values_are_coerced_so_numbers_and_flags_are_not_strings():
    parsed = _parse_options(["timeout=30", "retries=2.5", "strict=true"])
    assert parsed == {"timeout": 30, "retries": 2.5, "strict": True}


def test_a_malformed_pair_names_the_flag_the_user_actually_typed():
    """Two flags share one parser, so a wrong message sends people to the wrong one."""
    with pytest.raises(SystemExit) as excinfo:
        _parse_options(["base_url"], "--model-option")
    assert "--model-option" in str(excinfo.value)


def test_the_run_parser_accepts_repeated_model_options():
    args = build_parser().parse_args(
        [
            "run",
            "a goal",
            "--model",
            "openai",
            "--model-option",
            "base_url=http://localhost:11434/v1",
            "--model-option",
            "api_key=none",
        ]
    )
    assert args.model_option == [
        "base_url=http://localhost:11434/v1",
        "api_key=none",
    ]


class _Sentinel(Exception):
    pass


async def test_the_options_reach_the_provider_constructor(monkeypatch):
    """The wiring, not just the parsing.

    The provider is built before the engine is opened, so a spy that raises from
    __init__ proves what the constructor was handed without needing a browser.
    """
    seen: dict = {}

    class SpyProvider:
        def __init__(self, **kwargs):
            seen.update(kwargs)
            raise _Sentinel

    # Swap the whole registry cmd_run looks up. Two wrinkles force this shape:
    # `anybrowser.cli.main` as an attribute resolves to the exported *function*,
    # not the module, so the module comes from sys.modules; and Registry is
    # slotted, so its `get` cannot be patched in place.
    import sys

    class FakeRegistry:
        def get(self, name: str) -> type:
            return SpyProvider

    monkeypatch.setattr(sys.modules["anybrowser.cli.main"], "model_registry", FakeRegistry())
    args = argparse.Namespace(
        engine="playwright",
        option=[],
        goal="a goal",
        model="openai",
        model_name="some-model",
        model_option=["base_url=https://example.invalid/v1", "api_key=k"],
        max_steps=1,
        confirm=False,
    )
    with pytest.raises(_Sentinel):
        await cmd_run(args)
    assert seen == {"base_url": "https://example.invalid/v1", "api_key": "k"}
