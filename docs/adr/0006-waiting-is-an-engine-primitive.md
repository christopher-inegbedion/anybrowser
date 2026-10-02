# ADR-0006: Waiting for the page is an engine primitive, not an agent tool

**Status:** Accepted · **Date:** 2026-10-02

## Context

An agent needs to wait. A request is in flight, a spinner is up, results have
not landed. Without a way to wait, the only move is to act again and hope —
which is how a run spends its budget on a page that was about to be ready.

The obvious place to put this is the agent's toolset, next to `click` and
`scroll`. [ADR-0005](0005-agent-is-a-reference-loop.md) says that is the wrong
place: the agent layer is a reference loop, and a capability added there serves
only that loop.

Waiting is also not new here. Every engine already computed a page fingerprint
privately — `_page_signature` on the Playwright engine, `_signature` on the
Chrome and Safari ones — because that is what [ADR-0002](0002-truthful-outcomes.md)
is built on: an action reports `changed` by comparing one across the call. Three
implementations of the same idea, all private, none reachable.

One detail decided the design. There are **two** fingerprints in this codebase
and they are not interchangeable:

- `Snapshot.signature`, derived from the element set by `collect-elements.js`
- the `read.js` `signature` op, which is the URL, the page text and the focused
  field

Measured: changing a page's title or a paragraph's text moves the second and
**not** the first. So a wait built on the element signature returns early
exactly when it matters most — a result landing as text adds no elements.

## Decision

Two members on `BrowserEngine`, neither abstract:

**`page_signature()`** promotes what all three engines already had. It must be
sensitive to content — text, URL, focus — not just structure. The default
implementation derives it from `snapshot()`, which every engine implements
anyway; the three bundled engines override it with one cheap page evaluation.

**`await_change(timeout, poll, since)`** is a default implementation that polls
`page_signature`. An engine with something better — a CDP lifecycle event — can
override. `since` lets a caller act first and then wait without racing its own
change.

A timeout returns `changed=False`, not an exception. Nothing happened is an
outcome, and raising would push callers into treating "not yet" as "broken".

**Neither is abstract, deliberately.** Making `page_signature` required would
break every existing backend, third-party ones included, for the sake of a
convenience built on top of it. An override is an optimisation, not a duty. This
is the pattern for anything added to a published plugin interface: ship a
working default, let implementers improve on it.

## Consequences

Good: every engine gains waiting, including ones nobody here wrote, with no
work. The daemon forwards it, so a `remote` engine waits too. Three private
helpers became one documented member. Three conformance tests now grade what
`changed` has always depended on, where previously nothing tested the
fingerprint directly.

Bad: two members on an interface whose narrowness is the point
([ADR-0003](0003-narrow-engine-interface.md)). The defence is that both were
already there in spirit — one three times over, privately — and that the
alternative was the same capability trapped in one loop.

Ugly: `Snapshot.signature` still exists and still means something different.
Its docstring now warns about the distinction, but a third-party engine author
may reasonably reach for the wrong one. The fix would be renaming one of them,
which is a breaking change deferred rather than declined.

## When we would revisit

If "has the page changed" turns out to need more than a string comparison —
per-frame signatures, or distinguishing *what* changed — this becomes a
structure rather than a fingerprint, and `await_change` grows predicates. That
would be a larger interface and wants its own decision.
