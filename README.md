# RelayKit

**A browser-automation and agent runtime you can take apart.** Three interfaces —
the browser, the transport, the model — each with a registry and a conformance
suite, so "implement your own" is something you can verify rather than hope for.

```python
from relaykit import open_engine

async with await open_engine("playwright") as engine:
    await engine.navigate("https://example.com")
    page = await engine.snapshot()
    await engine.click(page.elements[0])
```

Same code against Chrome attached to your own logged-in window, against Safari,
or against a browser nobody has written a backend for yet.

---

## Why this exists

Most browser automation assumes it owns the browser. RelayKit assumes it does
not. It grew out of [Relay](https://relaythis.com), an agent that had to drive
the user's *real* Chrome and Safari windows — their tabs, their sessions, no
clean automation profile — and the abstractions here are the ones that survived
contact with that.

Three ideas do most of the work:

**Backends differ in kind, not quality.** Safari has no CDP; Apple's Web
Inspector protocol needs private entitlements. WebDriver cannot adopt your open
window. So an engine *declares* what it can do and callers route around the
gaps, instead of discovering them by catching an exception halfway through a
task. See [capabilities](docs/architecture/capabilities.md).

**A no-op is not a success.** The most common way an agent dies is a click that
hit nothing, reported as "success", read back by the model as progress, and
repeated forever. Every action returns `changed` alongside `ok`, and the
conformance suite fails an engine that clicks dead space and calls it a change.
See [truthful outcomes](docs/architecture/truthful-outcomes.md).

**The contract is executable.** `pytest --pyargs relaykit_conformance --engine
yours` is the definition of a working backend.

---

## Install

```bash
pip install relaykit[playwright]      # the reference engine, easiest start
pip install relaykit[chrome]          # attach to your own Chrome
pip install relaykit[all]             # everything
```

Python 3.10+. `import relaykit` pulls in no browser driver, no HTTP server and
no LLM SDK — every backend is an extra.

---

## The three interfaces

| You want to | Implement | Registry group | Ships with |
|---|---|---|---|
| Drive a different browser | [`BrowserEngine`](src/relaykit/core/engine.py) | `relaykit.engines` | `playwright`, `chrome`, `safari` |
| Change how clients reach the daemon | [`DaemonTransport`](src/relaykit/daemon/transport.py) | `relaykit.transports` | `websocket`, `unix`, `memory` |
| Use a different model | [`ModelProvider`](src/relaykit/models/provider.py) | `relaykit.models` | `openai`, `anthropic` |

Register with an entry point and yours is selectable by name everywhere:

```toml
[project.entry-points."relaykit.engines"]
firefox = "my_package.engine:FirefoxEngine"
```

```bash
pytest --pyargs relaykit_conformance --engine firefox
```

The suite is capability-gated. A backend that honestly declares it cannot drag
is skipped, not failed. A backend that *claims* it can drag and then doesn't is
failed — the suite tests truthfulness as hard as it tests function.

Full walkthrough: [**Writing an engine**](docs/guides/writing-an-engine.md).

---

## Engine status

| Engine | Attaches to your session | Trusted input | Conformance |
|---|---|---|---|
| `playwright` | no — own profile | yes | passing |
| `chrome` | yes — extension-owned CDP | yes | porting ([#1](https://github.com/relaykit/relaykit/issues/1)) |
| `safari` | yes — accessibility + extension | yes | porting ([#2](https://github.com/relaykit/relaykit/issues/2)) |

`chrome` and `safari` are being ported out of Relay's daemon. Their interfaces,
entry points and declared capabilities are fixed; the implementations are
landing. They refuse cleanly from `probe()` until they work, rather than
half-running — see [porting](docs/porting/).

---

## Layout

```
src/relaykit/
  core/          interfaces and value types — imports no backend
  engines/       chrome, safari, playwright
  perception/    turning a page into a snapshot, engine-agnostic
  daemon/        protocol, transports, the server that owns an engine
  agent/         planner, tools, executor, memory
  models/        LLM providers
conformance/     the executable contract
docs/            architecture, guides, ADRs
```

---

## Docs

- [Architecture overview](docs/architecture/README.md)
- [Capabilities](docs/architecture/capabilities.md) — why backends declare instead of raise
- [Truthful outcomes](docs/architecture/truthful-outcomes.md) — why `changed` exists
- [Writing an engine](docs/guides/writing-an-engine.md)
- [Writing a transport](docs/guides/writing-a-transport.md)
- [Writing a model provider](docs/guides/writing-a-model-provider.md)
- [ADRs](docs/adr/) — the decisions and what they cost

---

## Contributing

Start with [CONTRIBUTING.md](CONTRIBUTING.md). Good first issues are labelled
[`good first issue`](https://github.com/relaykit/relaykit/labels/good%20first%20issue);
a new backend is the highest-value contribution there is, and the conformance
suite means you can tell when it's done.

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
Security reports go to [SECURITY.md](SECURITY.md), not the issue tracker.

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
