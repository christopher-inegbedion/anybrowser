# Contributing to RelayKit

The most valuable thing you can contribute is **a backend for a browser we don't
support**, because the conformance suite means neither of us has to argue about
whether it works.

## Setup

```bash
git clone https://github.com/christopher-inegbedion/relaykit && cd relaykit
uv venv && uv pip install -e ".[dev]"
uv run playwright install chromium
uv run pytest                                             # unit tests
uv run pytest --pyargs relaykit_conformance --engine playwright   # the contract
```

## Before you open a PR

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
```

CI runs exactly these. If they pass locally they pass there.

## What a good PR looks like

**One change.** A bug fix and a refactor in the same diff take three times as
long to review and get reverted together when one of them is wrong.

**Tests that would have caught the bug.** Not tests that read back what the code
just set — a test that asserts `element.hidden is True` right after setting it
proves nothing. Assert the observable behaviour.

**Comments that explain why, not what.** The codebase leans on prose to record
what was measured and what was tried; `# increment i` is noise, `# buttons=1 is
required or HTML5 DnD silently no-ops` is the reason the line exists.

**A changelog entry** in `CHANGELOG.md` under `Unreleased`, for anything a user
would notice.

## Adding an engine

Read [docs/guides/writing-an-engine.md](docs/guides/writing-an-engine.md). The
short version:

1. Subclass `BrowserEngine`, implement the abstract methods.
2. Declare only capabilities you actually have. Under-claiming skips tests;
   over-claiming fails them. Both are fine, only one is dishonest.
3. `pytest --pyargs relaykit_conformance --engine yours` until green.
4. Ship it as your own package with a `relaykit.engines` entry point — you do
   not need to vendor it here. Open an issue and we'll link it from the README.

An engine lives in this repo only if it is one we commit to maintaining.

## Changing an interface

`core/engine.py`, `daemon/protocol.py` and `models/provider.py` are contracts
other people's code depends on. Changing one needs:

- an [ADR](docs/adr/) saying what the alternatives were and what this costs;
- a conformance-suite change in the same PR, since the suite *is* the contract;
- for `protocol.py`, a `PROTOCOL_VERSION` bump if the change is breaking.

Discuss it in an issue first. A PR that changes an interface without an ADR will
be asked for one before review, which wastes your time and ours.

## Commit messages

Conventional Commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`.
The scope is the subsystem — `fix(chrome): set buttons=1 on drag moves`.

## Reporting bugs

Use the issue templates. For an engine bug, say which engine and include the
conformance output — a failing conformance test is the best bug report there is.

Security issues go to [SECURITY.md](SECURITY.md), never to the tracker.

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
