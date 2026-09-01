# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Pre-1.0, the interfaces in `core/engine.py`, `daemon/protocol.py` and
`models/provider.py` may change in a minor release. Each such change gets an ADR
and a `BREAKING:` entry here.

## [Unreleased]

### Added

- `BrowserEngine`, the browser backend interface, with capability declaration
  rather than exception-driven discovery ([ADR-0001](docs/adr/0001-capabilities-over-exceptions.md)).
- `DaemonTransport` and a JSON-framed protocol, so the daemon does not know
  whether it is reached over a socket, a pipe, or nothing at all.
- `ModelProvider`, the LLM interface.
- Entry-point registries for engines, transports and models.
- `relaykit_conformance`, the executable engine contract — 32 tests, capability
  gated, installed as a pytest plugin so third-party backends can run it in
  their own repo with one command.
- `PlaywrightEngine`, the reference backend. Passes conformance.
- `ChromeEngine` over the DevTools WebSocket: 28 passed, 4 skipped. Launches
  Chrome or attaches to a running one, and derives its declared
  `attach_to_user_session` from what the live pipe can actually reach.
- `ChromeEngine`, a direct CDP backend with truthful action outcomes, DOM
  perception, trusted input, navigation, tabs, screenshots, uploads, and cookies.
- `SyncEngine`, a blocking facade owning exactly one event loop.
- Three transports — `memory`, `unix`, `websocket` — each shipping a server and
  its matching client, and each passing the 10-test transport contract.
- `relaykit.perception`: engine-agnostic DOM perception, with the deep-DOM
  helpers (open and closed shadow roots, iframe coordinate mapping) ported from
  Relay.
- `CdpConnection`, the seam between the Chrome engine and its pipe, so the
  DevTools WebSocket and extension-owned CDP share one engine.
- `SafariBridge` and the Swift accessibility helper, with the host bundle
  identifier as a build parameter — see `build_engine`.

### Fixed

- `relaykit.engines` (the subpackage) shadowed the engine registry re-exported
  under the same name, so `available_engines()` raised on a fresh install while
  working in a checkout.
- `ActionOutcome.failure` was called with `detail` both positionally and by
  keyword in the Playwright engine's `select_option`, which would have raised
  `TypeError` on any unmatched option.
- `press_key` on Chrome sent text-bearing keys as `rawKeyDown`, stalling the
  input queue so the *next* command hung until timeout.

### In progress

- `ChromeEngine` in `extension` mode — CDP relayed through a browser extension,
  the mode that reaches the user's own window.
- `SafariEngine` — the native half is done; perception needs the Web Extension.
- The daemon server.
- The agent runtime: planner, tools, executor, memory.
