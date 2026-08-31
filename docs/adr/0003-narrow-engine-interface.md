# ADR-0003: A narrow action-level engine interface, not a Page object

**Status:** Accepted · **Date:** 2026-08-31

## Context

Playwright's `Page` is the de facto standard shape for browser automation, and
the obvious move was to make `BrowserEngine` look like it: `locator()`,
`frame()`, `evaluate_handle()`, `wait_for_load_state()`, chained element
handles. Everything already written against Playwright would then port for free.

Relay did exactly this. Its CDP backend implements a `PageFacade` presenting the
Playwright `Page` surface over a raw debugger connection. It is 1,480 lines,
most of them reproducing behaviour that only exists because Playwright is
structured the way it is.

Then Safari arrived. Safari has no CDP and no remote-object model. There is no
`evaluate_handle`. There is no persistent element handle to chain from. A
Playwright-shaped interface is not merely awkward to implement there — a large
part of it has no meaning.

## Decision

The interface is a flat set of actions over `Element` and `Point` values. No
element handle objects, no locators, no chaining. `Element.handle` is an opaque
engine-owned string, valid until navigation, that callers pass back verbatim.

## Alternatives considered

**Mirror the Playwright `Page` API.** Rejected. It admits exactly one class of
backend — remote-object protocols — and Safari, accessibility-driven engines,
and anything extension-based cannot implement it without a large lying adapter.
The 1,480-line facade was the evidence.

**Two interfaces: a rich one and a minimal one.** Rejected: every caller would
target the rich one, the minimal one would rot, and we would be back here with
extra steps.

**Selector strings as the addressing primitive.** Rejected: it pushes element
resolution into every backend and makes it impossible to address something the
page has no stable selector for — canvas cells, shadow-DOM internals, an
accessibility node with no DOM counterpart.

## Consequences

Good: Safari and accessibility-driven backends are first-class rather than
second; the interface is small enough to hold in your head and to implement in
an afternoon; the conformance suite can cover it exhaustively.

Bad: code written against Playwright does not port for free. We ship
`PlaywrightEngine` partly to absorb that.

Ugly: opaque handles mean callers cannot inspect what they are addressing, and a
handle that outlives its page is a class of bug that did not exist with live
element objects. `StaleHandle` and the generation counter in the reference
engine are the mitigation, and it is not free.
