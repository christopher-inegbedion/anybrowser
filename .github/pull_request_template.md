## What this changes

<!-- One or two sentences. If it needs more, it is probably two PRs. -->

## Why

<!-- The problem, not the patch. What breaks today. -->

## Checklist

- [ ] `uv run ruff check . && uv run ruff format --check .`
- [ ] `uv run mypy`
- [ ] `uv run pytest`
- [ ] `uv run pytest --pyargs relaykit_conformance --engine playwright`
- [ ] Tests that would have caught this, asserting observable behaviour rather
      than reading back what the code just set
- [ ] `CHANGELOG.md` updated under `Unreleased`, if a user would notice
- [ ] An [ADR](docs/adr/) if this changes `core/engine.py`,
      `daemon/protocol.py` or `models/provider.py`
