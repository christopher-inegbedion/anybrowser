# AnyBrowser

**A browser-automation and agent runtime you can take apart.** Three interfaces —
the browser, the transport, the model — each with a registry and a conformance
suite, so "implement your own" is something you can verify rather than hope for.

```python
from anybrowser import open_engine

async with await open_engine("playwright") as engine:
    await engine.navigate("https://example.com")
    page = await engine.snapshot()
    await engine.click(page.elements[0])
```

Same code against Chrome attached to your own logged-in window, against Safari,
or against a browser nobody has written a backend for yet.

---

## Why this exists

Most browser automation assumes it owns the browser: a fresh profile, a clean
window, a driver launched with the right flags. That assumption breaks the
moment the job is to work in the browser a person actually uses — their tabs,
their sessions, their logins, already open. You cannot ask them to log in again
inside a throwaway profile, and you cannot attach WebDriver to a window that was
not started for automation.

AnyBrowser assumes it does not own the browser. Every abstraction here comes from
driving real Chrome and Safari windows in production, and every one of the three
ideas below is a failure mode that cost someone a week.

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

**The contract is executable.** `pytest --pyargs anybrowser_conformance --engine
yours` is the definition of a working backend.

---

## Install

```bash
pip install anybrowser[playwright]      # the reference engine, easiest start
pip install anybrowser[chrome]          # attach to your own Chrome
pip install anybrowser[all]             # everything
```

Python 3.10+. `import anybrowser` pulls in no browser driver, no HTTP server and
no LLM SDK — every backend is an extra.

The Playwright engine needs its browser binaries once, which pip cannot fetch
for you:

```bash
playwright install chromium
```

Running a contract against something whose extra is missing skips rather than
fails, and says which extra to install — so `pip install anybrowser[all]` is the
shortcut if you would rather not think about it.

---

## The three interfaces

| You want to | Implement | Registry group | Ships with |
|---|---|---|---|
| Drive a different browser | [`BrowserEngine`](src/anybrowser/core/engine.py) | `anybrowser.engines` | `playwright`, `chrome`, `safari` |
| Change how clients reach the daemon | [`DaemonTransport`](src/anybrowser/daemon/transport.py) | `anybrowser.transports` | `websocket`, `unix`, `memory` |
| Use a different model | [`ModelProvider`](src/anybrowser/models/provider.py) | `anybrowser.models` | `openai`, `anthropic` |

Register with an entry point and yours is selectable by name everywhere:

```toml
[project.entry-points."anybrowser.engines"]
firefox = "my_package.engine:FirefoxEngine"
```

```bash
pytest --pyargs anybrowser_conformance --engine firefox     # the engine contract
pytest --pyargs anybrowser_conformance --transport grpc     # the transport one
```

Each flag selects a contract and skips the other. The suite is capability-gated. A backend that honestly declares it cannot drag
is skipped, not failed. A backend that *claims* it can drag and then doesn't is
failed — the suite tests truthfulness as hard as it tests function.

Full walkthrough: [**Writing an engine**](docs/guides/writing-an-engine.md).

---

## Engine status

| Engine | Attaches to your session | Trusted input | Engine contract | Gated by CI |
|---|---|---|---|---|
| `chrome` (DevTools) | no — needs the launch flag | yes | **28 passed, 4 skipped** | yes |
| `chrome` (extension) | **yes** — your own windows | yes | **28 passed, 4 skipped** (2026-10-02, Chrome 154) | no — needs a browser with the extension loaded |
| `playwright` | no — own profile | yes | **28 passed, 4 skipped** | yes, the reference engine |
| `safari` | yes — accessibility + extension | yes | **25 passed, 7 skipped** (2026-10-02, Safari 26.5, macOS 26.5) | only that it refuses cleanly off-setup |

The engine contract is 32 tests. The 4 skips are the capability gate working as
intended: those tests assert the *error* an engine raises for something it does
not declare, so they stand down for an engine that declares it. Run the whole
suite directory and you will see more skips still — the transport and model
contracts, idle because you passed neither `--transport` nor `--model`.

Pointing the engine contract at the `remote` engine grades the whole daemon
stack — transport, protocol, codec, dispatch — and it scores the same **28
passed, 4 skipped** over both the memory and websocket transports.

The Chrome engine has two pipes behind one interface. `devtools` talks to a
browser started with `--remote-debugging-port` — standard, and the only mode
that runs in CI. `extension` relays CDP through a browser extension and is the
one that matters, because it attaches to the browser you already have open,
with your tabs and your logins. Same engine either way; the connection reports
which it is, and the declared `attach_to_user_session` capability is derived
from that rather than asserted next to it.

Both Chrome pipes pass the contract. To drive the browser you already have
open, see [driving your own Chrome](docs/guides/chrome-extension.md) — note that
`--load-extension` is silently ignored by Chrome stable, which is a memorable
afternoon if nobody tells you.

Safari is implemented in both halves — a Swift helper for trusted background
input and occlusion-proof capture, and a Web Extension for the DOM and pointer
gestures — and it passes the contract, three runs in a row. Clicks carry real
user activation (`isTrusted=true`), which is the whole reason the native half
exists. Seven tests stand down: `file_upload`, `full_page_screenshot` and
`cross_origin_frames` are honestly undeclared, and four are the capability gate.

Uploads are the notable absence. A file input's `files` cannot be set from
JavaScript, by design, and without a debugger protocol there is no counterpart
to CDP's `DOM.setFileInputFiles`. The accessibility path can only open the
system picker and drive it, which is a picker — so this engine says it cannot,
rather than appearing to and failing on a real form.

Two things that setup cannot be done without, and neither is optional:

- **The containing app must be signed with a real identity.** An ad-hoc
  signature (`CODE_SIGN_IDENTITY=-`) makes the extension invisible to Safari —
  it never appears in Settings ▸ Extensions, nothing is logged, and "Allow
  unsigned extensions" does not cover it. Sign with an Apple Development
  identity and install the app under `/Applications`.
- **Safari loads extension background content lazily.** The bridge dials the
  engine when its background page loads, and Safari does not load it just
  because the extension is enabled, so the engine waits and then refuses. Until
  that is fixed, wake it from Develop ▸ Web Extension Background Content before
  a run; it shows as "(not loaded)" when asleep.

---

## Command line

```bash
anybrowser plugins                        # what is installed
anybrowser info --engine chrome           # what that engine can do
anybrowser look https://example.com       # open a page, print what is on it
anybrowser serve --transport unix         # a daemon owning one browser
anybrowser run "find the pricing page"    # give an agent a goal
```

`run` takes a provider and its constructor options, so any OpenAI-compatible
endpoint works — a gateway, a local vLLM, Ollama:

```bash
anybrowser run "read the top pricing tier" \
  --engine playwright \
  --model openai --model-name google/gemini-3.5-flash \
  --model-option base_url=https://openrouter.ai/api/v1 \
  --model-option api_key=$OPENROUTER_API_KEY
```

`--confirm` asks before every action that changes the page, and `--max-steps`
caps the run. Engine options use `-o KEY=VALUE`, for example
`-o headless=true`.

---

## Layout

```
src/anybrowser/
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
- [Driving your own Chrome](docs/guides/chrome-extension.md)
- [Driving Safari](docs/guides/safari-extension.md)
- [ADRs](docs/adr/) — the decisions and what they cost

---

## Contributing

Start with [CONTRIBUTING.md](CONTRIBUTING.md). Good first issues are labelled
[`good first issue`](https://github.com/christopher-inegbedion/anybrowser/labels/good%20first%20issue);
a new backend is the highest-value contribution there is, and the conformance
suite means you can tell when it's done.

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
Security reports go to [SECURITY.md](SECURITY.md), not the issue tracker.

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
