# Architecture

RelayKit is four layers, each written against the interface below it and nothing
else. That is the whole design; everything else follows from it.

```
       agent/        planner, tools, executor, memory
          |            "decide what to do, then do it"
          v
      daemon/        owns one engine, serves many clients
          |            "one browser, several things wanting it"
          v
    perception/      page -> Snapshot
          |            "what is on this page and where"
          v
      engines/       BrowserEngine: chrome | safari | playwright | yours
                       "make the browser do a thing"
```

Every arrow is an interface with an entry-point registry behind it, so any layer
can be replaced without touching the ones above.

## The layers

### `core/` — interfaces and values

Imports no backend, no driver, no HTTP library. If `core` ever needs to import
`websockets` or `playwright`, an abstraction has failed and the fix is upstream
of that import.

- [`engine.py`](../../src/relaykit/core/engine.py) — `BrowserEngine`, the contract.
- [`types.py`](../../src/relaykit/core/types.py) — `Snapshot`, `Element`, `Point`, `ActionOutcome`.
- [`errors.py`](../../src/relaykit/core/errors.py) — what every backend translates its failures into.
- [`registry.py`](../../src/relaykit/core/registry.py) — entry-point plugin discovery.

### `engines/` — making a browser do things

An engine is a live connection to one browser, addressing one active tab. It
does not know what a goal is, does not decide anything, and does not manage
several tabs at once — that is the daemon's job, done once for every backend
rather than once per backend.

The three shipped engines exist for different reasons:

| Engine | Why it exists |
|---|---|
| `chrome` | The real one. Extension-owned CDP, so it attaches to the user's own window with their sessions. |
| `safari` | Same goal, no CDP available: accessibility for trusted input, a Web Extension for perception. |
| `playwright` | The reference. Shortest complete implementation, the file to copy, the one CI runs. |

### `perception/` — page to `Snapshot`

Turning a live page into a value a planner can reason about: which elements are
interactive, where they are, what they say. Engine-agnostic — it asks the engine
to evaluate script or to read its accessibility tree, and builds the same
`Snapshot` either way.

This is where the *quality* of an agent mostly lives. A planner is only as good
as what it can see.

### `daemon/` — one browser, many clients

The browser is a singleton and several things want it: the CLI, a UI, a
scheduled run. The daemon owns one engine and serves them, handling tab
ownership, event fan-out and authentication.

It knows nothing about sockets. [`DaemonTransport`](../../src/relaykit/daemon/transport.py)
is the seam, and the shipped transports are WebSocket, Unix socket, and an
in-process one used by tests.

### `agent/` — deciding what to do

A `Planner` proposes a `Decision`, an executor runs the matching `Tool`, and the
result goes into history for the next step. Swapping the planner changes what
kind of agent this is; the tools, engines and daemon underneath do not move.

## Reading order

If you're new, read in this order — each one only needs the one before it:

1. [`core/types.py`](../../src/relaykit/core/types.py) — the vocabulary
2. [`core/engine.py`](../../src/relaykit/core/engine.py) — the contract
3. [`engines/playwright/engine.py`](../../src/relaykit/engines/playwright/engine.py) — the contract, satisfied
4. [`conformance/relaykit_conformance/test_contract.py`](../../conformance/relaykit_conformance/test_contract.py) — the contract, enforced

## The two ideas worth arguing about

- [Capabilities, not exceptions](capabilities.md)
- [Truthful outcomes](truthful-outcomes.md)

Both came out of production failures rather than taste, and both are recorded as
[ADRs](../adr/) with what they cost.
