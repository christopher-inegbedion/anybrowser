# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Pre-1.0, the interfaces in `core/engine.py`, `daemon/protocol.py` and
`models/provider.py` may change in a minor release. Each such change gets an ADR
and a `BREAKING:` entry here.

## [Unreleased]

### Added

- `anybrowser_bench`, a seeded task suite that grades **agents** the way
  conformance grades engines. It has eight task families: waiting, forms,
  scrolling, pagination, a cookie wall, multi-page navigation, delayed search,
  and an impossible task. Every answer is drawn from a seed, so it cannot be
  memorised or guessed from what a page usually says. The judge never takes the
  agent's word for anything: answers are checked against the seed, and form
  tasks against what the bench server actually received. A confident wrong
  answer scores -1, below an honest failure at 0, so a fitness score built on it
  cannot be raised by guessing. Run it with `python -m anybrowser_bench`, or call
  `run_suite(agent, engine)` with any `(engine, goal) -> RunResult` callable.
- A scripted solver scores +1.000 on Chrome, which shows every task can be solved
  and that the judge agrees with the server. claude-sonnet-5 and claude-opus-5.5
  also score +1.000 on 2 seeds per task. The suite tests basic competence and has
  no harder tier yet.
- Local baselines through Ollama, on 2 seeds per task: qwen3:4b scores +0.938
  (15 of 16, using `--max-tokens 8192`), and gemma3:1b scores 0.000, because it
  never produces a usable action.
- An `error` outcome for attempts where the model server or the browser broke.
  These attempts are not scored, a report that contains any of them is marked
  untrustworthy, and the suite stops after three in a row. The first baseline
  counted a misconfigured endpoint as sixteen honest failures, fitness 0.000,
  and an evolution loop would have ranked that.
- `ModelOutputError`, a subclass of `ModelError`, for a model that answered but
  not in a form the planner can use. `RunResult.error_type` records which
  exception class ended a run, so the bench counts an unusable reply against
  the model and excludes only real infrastructure failures.
- `--max-tokens` for the bench. A reasoning model thinks inside this budget.

- An image-only reference agent
  ([ADR-0008](docs/adr/0008-an-image-only-reference-agent.md)): `VisionPlanner`
  and `vision_tools()`. The model sees only the screenshot, shown at the
  viewport's CSS size, and acts in screenshot pixels: click, type, scroll, press
  a key, wait. It does not see the DOM, the page text or the URL. Run it with
  `--vision` in the bench.
- `ChromeEngine(viewport_width=, viewport_height=)`. A browser the engine
  launches defaults to 1280x800, matching Playwright. A browser it attaches to
  is left alone unless a size is given. The bench asks for 1280x800 explicitly
  from any engine that takes the option.
- Two canvas tasks, `canvas-menu` and `canvas-read`, where the page is a single
  `<canvas>` with no text, so only an agent that looks can pass them.
- `python -m anybrowser_bench.grounding`, which measures whether a model can
  point at a named target on a screenshot, scored against the real boxes.
  qwen2.5vl:3b: 29/34 through the provider, 10/10 on canvas.
- Constrained replies
  ([ADR-0009](docs/adr/0009-constrained-replies-are-a-provider-capability.md)):
  `ModelProvider.structured_output(schema)`, which is not abstract and defaults
  to `None`. The OpenAI-compatible provider implements it with `response_format`.
  With `constrained=True` the planner sends one flat schema per step (narrative,
  tool, arguments, answer, with "done" as a tool), narrowed to the page: handles
  must be in the current listing, and vision coordinates must be inside the
  screenshot. Use `--constrained` in the bench.
- Training data from the agent's own runs. `--record DIR` keeps every model
  exchange verbatim: the system prompt, the user message, the screenshot, the
  request options and the reply, alongside the judge's outcome.
  `python -m anybrowser_bench.dataset DIR --out DS` builds a fine-tuning set in
  the format `mlx_vlm.lora` loads. It keeps only attempts the judge scored a
  success, and within them only steps the engine said changed the page, plus
  the final "done", so the model is not trained on the repeated no-effect
  clicks that sank the baseline. `--temperature` makes repeated attempts differ.
- `python -m anybrowser_bench.improve`, which runs self-improvement rounds
  unattended: serve the model (plus the previous adapter) with mlx-vlm on
  localhost, evaluate on held-out seeds, collect runs on fresh seeds at a
  temperature above 0, stop the server, build a dataset from every judge-verified
  success so far, and train a LoRA from the base weights. There is no teacher
  model; the only signal is the judge. Every phase leaves a file behind, so an
  interrupted run resumes where it stopped. Verified with a one-round smoke run
  on 2 tasks, including that the next round's server really loaded the adapter.
- A `train` extra (`mlx-vlm[train]`, Apple Silicon only). Verified end to end on
  an M4 with 16 GB: LoRA on the 4-bit MLX Qwen2.5-VL-3B trains on these rows
  with full 1280x800 screenshots, using `--grad-checkpoint` and with Ollama's
  models unloaded. It peaks at 8.9 GB and takes 25-50s per step, about 2,270
  tokens per example.
- The constrained reply now makes finishing an explicit `done` boolean, decided
  before the tool. With "done" as the last tool name, qwen2.5vl on MLX wrote the
  right answer ("7030") beside `"tool": "type_text"` for four steps running and
  never finished: 0 successes in about 24 held-out attempts. With the boolean, the
  same tasks and seeds give 5 of 10. It now finishes too eagerly instead (5 false
  claims). Those score -1 and never enter a training set.
- Key names from models are normalised in `press_key`: "enter", "RETURN" and
  "esc" become "Enter" and "Escape". qwen2.5vl's search failed on
  "unsupported key (key='enter')".
- `Attempt.trace`, which records every step: tool, arguments, narrative, and
  whether the engine said it changed anything.

### Fixed

- A click that redrew a canvas was reported as "click hit nothing", because the
  page signature had no way to see pixels. The largest two canvases are now
  fingerprinted coarsely (48x30, four bits per channel), so a redrawn screen
  counts as a change and a still canvas does not. Applied to the shared page
  script, the Playwright script and the Safari extension. Conformance tests
  cover both sides; Safari is untested, as before.
- A planner reply that hit the token limit before any visible output was
  reported as "did not return JSON: ''". It now uses the provider's stop reason
  and says the reply was cut off. qwen3:4b ran into this on a third of its steps
  at the default 2048 tokens.
- The model providers reported a bad constructor argument as the API returning
  invalid JSON. They now convert `timeout` to a number when constructed, or name
  the bad value, and only an unparseable response body is called invalid JSON.

## [0.3.0] - 2026-10-03

The agent can wait. A result that lands a second after a click used to leave
it one move -- act again and hope. Waiting is now an engine primitive
([ADR-0006](docs/adr/0006-waiting-is-an-engine-primitive.md)), so every backend
has it and the conformance suite grades it, and the reference agent reaches it
through a `wait` tool. Safari's first run of a session is no longer a coin toss.

`BrowserEngine` gains two members. Neither is abstract, so existing engines,
third-party ones included, keep working unchanged. `Snapshot.signature` is
renamed `element_signature`, so the two page fingerprints can no longer be
mistaken for each other. The old name still works, with a warning.

### Added

- `BrowserEngine.await_change(timeout, poll, since)` and
  `BrowserEngine.page_signature()`, neither abstract
  ([ADR-0006](docs/adr/0006-waiting-is-an-engine-primitive.md)). Waiting for an
  in-flight result to land is the one thing an agent cannot otherwise do, and it
  belongs to browsers rather than to one loop. `page_signature` promotes what
  all three engines already computed privately -- it is what `changed` has
  always been built on -- with a default derived from `snapshot()` so no backend
  has to implement either one. The daemon forwards it, so a `remote` engine
  waits too.
- Three conformance tests for it, including one that grades the fingerprint
  directly: a signature that moves for an element change but not for a
  text-only change fails, because a wait built on it returns early exactly when
  a result lands as text. Nothing tested that before.
- `pages/async.html`, a fixture whose text changes 700ms after a click, adding
  and removing no elements.
- A `wait` tool, so the agent can actually use `await_change`. Until now
  the engine could wait and the agent had no way to ask it to. The tool is a
  thin wrapper: it caps the timeout at 15s and picks the baseline. The baseline
  is the page *as the planner last saw it*, recorded by the runner as
  `ToolContext.page_signature` before each snapshot. A model spends seconds deciding
  to wait, and that is when results land, so measuring from when the tool runs
  would miss them, report that nothing changed, and be contradicted by the next
  observation. The tool is not mutating, so confirmation does not gate it. A wait
  that times out still counts toward the stuck breaker, so a run cannot wait out
  its budget.

### Deprecated

- `Snapshot.signature` is renamed `Snapshot.element_signature`
  ([ADR-0007](docs/adr/0007-element-signature-is-named-for-what-it-measures.md)).
  It fingerprints the element set and does not move when only text changes, so
  it cannot answer "did the page change", which is what
  `BrowserEngine.page_signature()` is for. The old name still works for both
  reading and constructing, and warns with a message that names the method you
  probably wanted. It will be removed before 1.0. The daemon sends both keys and
  reads either, so mixed versions keep the field without a protocol bump.

### Fixed

- Safari: the first run of a session failed tests that passed on every run
  after it. `start()` only needs the background page to answer `tabs`, so it
  returned while the content script in the target tab was still coming up, and
  whatever acted first lost the race. It now waits for the page to be
  answering, which is not fatal if it never does -- native input works on a page
  the extension cannot see, and the first perception call explains itself.

## [0.2.0] - 2026-10-02

Safari works. In 0.1.0 its clicks silently never fired and every action reported
`changed=False`, which is this project's own headline principle inverted. The
agent could not read page text at all. The CLI could not be pointed at any
model gateway. All of that is fixed below, and the Safari engine now passes its
contract on a real machine.

### Added

- [ADR-0005](docs/adr/0005-agent-is-a-reference-loop.md): the agent runtime is a
  reference loop, not the product. It exists to show the three plugin interfaces
  composing, and a small tool surface is correct rather than incomplete.
  Capabilities belong to the layers that are contract-graded -- one added to the
  loop is available only to the loop, while the same one on `BrowserEngine` is
  available to every loop and enforced by the suite.

### Removed

- The `look` tool. `AgentRunner` re-observes before every decision -- its
  docstring says so and `_observe()` does it -- so `look` fetched a snapshot
  that had already been taken for that same step. It cost a step, counted
  toward the stuck breaker, and returned what the planner was holding anyway;
  an agent asked to read a page would call it repeatedly and be stopped as
  stuck. The prompt no longer suggests it, and says the page is re-read for
  you.

### Fixed

- The planner never showed the page's **text**. `Snapshot.text` has been
  populated all along -- `snapshot(include_text=True)` is the default -- and the
  prompt renderer printed the URL, the title and the element list, then dropped
  it. So an agent could see what was clickable and never what the page said:
  any goal of the form "tell me what it says" was unreachable, and a search
  that worked could not report its results. Rendered now, capped, with the cut
  marked so a model knows there is more rather than reading a sentence that
  stops mid-word as the end of the page.


- Safari: **`AXManualAccessibility` was never set**, so WebKit handed out an
  accessibility tree that could be read but not acted on. Roles, titles and
  frames resolved, `AXPress` appeared in the element's actions, performing it
  returned `.success` — and the page saw nothing. Every observable signal said
  the click had worked. This is the opt-in every assistive client uses to get a
  live tree out of a WebKit app; clicks now carry real user activation
  (`isTrusted=true`).
- Safari: `press_key("Enter")` ran `keystroke "Enter"`, which types the five
  characters E, n, t, e, r. Named keys now go through `key code`. Modifiers were
  broken the same way — `alt` and `meta` mean nothing to System Events, which
  wants `option` and `command`, and a wrong name is a syntax error that took the
  whole keypress with it.
- Safari: a click on dead space raised instead of reporting `changed=False`. The
  helper now answers `pressed: false` with a reason, because "there was nothing
  to press" is an outcome and an exception gives the caller nothing to report. A
  press that is attempted and fails still raises.
- Safari: `_read` assumed every page reply was a mapping and called `.get` on
  it, which raised on the ops that answer with a scalar. In `_signature` that
  was swallowed, so every signature read returned `""`, two empty strings
  compared equal, and **every action reported `changed=False`** — the exact
  failure ADR-0002 exists to prevent, arriving through the machinery meant to
  prevent it.
- Safari: `select_option` went through that same unwrapping, which strips a
  mapping to its `value` field — and this op answers with an object that
  *carries* `value` next to `ok`, so the check read `.get` on a bare string.

### Changed

- Safari no longer declares `FILE_UPLOAD`. It was declared with no
  implementation behind it: a file input's `files` cannot be set from
  JavaScript, and without a debugger protocol there is no counterpart to
  `DOM.setFileInputFiles`. A declared capability with nothing behind it is the
  one thing [ADR-0001](docs/adr/0001-capabilities-over-exceptions.md) exists to
  prevent, and the conformance suite caught it.


- Safari: the extension channel coerced every reply's `result` to a mapping,
  but some commands answer with a scalar -- `read` with `op="href"` returns the
  URL as a bare string, and `dict("https://...")` raises. This failed 8 of the
  32 contract tests on its own; the callers already expected the raw shape.
- Safari: the helper's stdio reader used asyncio's default 64KiB line limit, so
  every screenshot failed with `LimitOverrunError`. A capture arrives as base64
  on one line and is megabytes.
- The Safari extension build gave its embedded extension the *host app's* bundle
  identifier, because `PRODUCT_BUNDLE_IDENTIFIER` passed to `xcodebuild` applies
  to every target. Xcode accepts it -- a string is a prefix of itself -- and
  `pluginkit` then registers the extension under the app's identity. The app's
  identifier is now rewritten in the generated project instead.
- The Safari extension build wrote into Xcode's shared DerivedData and printed
  no path. It now builds into the output directory and prints where the app is.

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
