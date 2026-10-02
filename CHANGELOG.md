# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Pre-1.0, the interfaces in `core/engine.py`, `daemon/protocol.py` and
`models/provider.py` may change in a minor release. Each such change gets an ADR
and a `BREAKING:` entry here.

## [Unreleased]

### Added

- `anybrowser run --model-option KEY=VALUE`, repeatable, passed to the model
  provider's constructor. Without it the CLI could only reach a provider's
  default endpoint, so the OpenAI-compatible provider -- which exists to be
  pointed at OpenRouter, vLLM or Ollama -- could not be used from the command
  line at all. The conformance plugin already had the equivalent flag.
- A `Command line` section in the README. The CLI shipped in 0.1.0 with five
  subcommands and no documentation.

## [0.1.0] - 2026-10-02

Published as `anybrowser`. The project was called RelayKit until first release;
PyPI blocks the name as too similar to an existing `relay-kit`, and since nothing
had shipped yet, renaming was cheaper than living with a confusable name in the
same niche. The import name, the CLI, the `anybrowser.*` entry-point groups and
the `ANYBROWSER_*` environment variables all moved with it. There is no
compatibility shim, because there is no previous release to be compatible with.

### Added

- `DaemonTransport.probe()`, mirroring `BrowserEngine.probe()`: a transport
  whose optional dependency is absent, or whose primitive the platform lacks,
  now refuses rather than failing mid-test. The transport contract skips on a
  refusal, so a fresh install no longer opens with eight red tests caused by an
  extra the user simply had not installed. Defaults to a no-op, so existing
  third-party transports are unaffected
  ([ADR-0001](docs/adr/0001-capabilities-over-exceptions.md)).

- `BrowserEngine`, the browser backend interface, with capability declaration
  rather than exception-driven discovery ([ADR-0001](docs/adr/0001-capabilities-over-exceptions.md)).
- `DaemonTransport` and a JSON-framed protocol, so the daemon does not know
  whether it is reached over a socket, a pipe, or nothing at all.
- `ModelProvider`, the LLM interface.
- OpenAI-compatible and Anthropic model providers with multimodal requests,
  real SSE streaming, and token-based cost reporting.
- Entry-point registries for engines, transports and models.
- `anybrowser_conformance`, the executable contracts — 32 engine tests, 10
  transport, 6 model — capability gated and installed as a pytest plugin, so a
  third-party backend runs them in its own repo with one command.
- `PlaywrightEngine`, the reference backend. Passes conformance.
- `ChromeEngine` over the DevTools WebSocket: 28 passed, 4 capability-gated
  skips. Launches
  Chrome or attaches to a running one, and derives its declared
  `attach_to_user_session` from what the live pipe can actually reach.
- `ChromeEngine`, a direct CDP backend with truthful action outcomes, DOM
  perception, trusted input, navigation, tabs, screenshots, uploads, and cookies.
- `SyncEngine`, a blocking facade owning exactly one event loop.
- Three transports — `memory`, `unix`, `websocket` — each shipping a server and
  its matching client, and each passing the 10-test transport contract.
- `anybrowser.perception`: engine-agnostic DOM perception, including deep-DOM
  helpers for open and closed shadow roots and iframe coordinate mapping.
- `CdpConnection`, the seam between the Chrome engine and its pipe, so the
  DevTools WebSocket and extension-owned CDP share one engine.
- `SafariBridge` and the Swift accessibility helper, with the host bundle
  identifier as a build parameter — see `build_engine`.

### Fixed

- `anybrowser.engines` (the subpackage) shadowed the engine registry re-exported
  under the same name, so `available_engines()` raised on a fresh install while
  working in a checkout.
- `ActionOutcome.failure` was called with `detail` both positionally and by
  keyword in the Playwright engine's `select_option`, which would have raised
  `TypeError` on any unmatched option.
- `press_key` on Chrome sent text-bearing keys as `rawKeyDown`, stalling the
  input queue so the *next* command hung until timeout.
- The `unix` transport now refuses with a clear message on platforms without
  Unix domain sockets, instead of raising `AttributeError` from inside a
  connect.

- `DaemonServer`: owns one engine, serves many clients, with pluggable
  authorisation (`AllowAll`, `TokenAuth`).
- `RemoteEngine`: the client end, which is itself a `BrowserEngine`. Registered
  as the `remote` engine, so `--via-daemon` runs the entire engine contract
  through the daemon stack.
- `scripts/check_entry_points.py`, run in CI: imports every declared plugin.
- Model providers: `openai` (any OpenAI-compatible endpoint) and `anthropic`,
  both with real streaming, real token counts and per-model pricing that
  reports zero *with an explanation* rather than inventing a free call.
- The agent runtime: `AgentRunner`, `LLMPlanner`, and eight browser tools.
- A model-provider conformance suite (`--model`), opt-in because it spends money.
- The `anybrowser` CLI: `plugins`, `info`, `look`, `serve`, `run`.

### Fixed

- The agent's stuck detector measured repetition, so the commonest loop shape —
  act, look, act, look — walked straight past it. It now measures progress:
  N consecutive actions that change nothing, whatever their shape.
- The planner rendered elements as `[2:0] a 'a Learn more'`, and models duly
  passed the quoted label as the handle. Handles now have their own labelled
  column, and the tag is not printed twice.

- `ChromeEngine(mode="extension")`: CDP relayed through the AnyBrowser browser
  extension, attaching to the browser the user already has open. Passes the
  full engine contract. The extension itself is in `extensions/chrome`.

- `SafariEngine`, complete: the Swift accessibility helper for trusted
  background input and occluded-window capture, plus a Safari Web Extension for
  the DOM and pointer gestures. `scripts/build_safari_extension.py` assembles
  and converts it in one command.

### In progress

- The agent runtime: planner, tools, executor, memory.
