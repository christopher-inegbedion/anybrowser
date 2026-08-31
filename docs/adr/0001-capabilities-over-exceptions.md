# ADR-0001: Engines declare capabilities instead of raising

**Status:** Accepted · **Date:** 2026-08-31

## Context

Browser backends differ structurally. Safari exposes no CDP — the Web Inspector
protocol requires private Apple entitlements — so network interception and
pre-navigation init scripts have no implementation, ever. WebDriver cannot adopt
an existing browser window, so it can never drive a user's logged-in session. A
content script cannot produce trusted input, so anything gated on user
activation silently does nothing.

Callers need to route around these. The question was when they find out.

## Decision

`BrowserEngine.capabilities` returns a declared set, answerable before `start()`.
Optional methods raise `CapabilityNotSupported` if called anyway, and that
exception means *never*, not *not right now*. Transient failures are
`ActionFailed`.

The conformance suite is capability-gated: a backend that does not declare
`POINTER_GESTURES` skips the drag tests; one that declares it and cannot drag
fails them.

## Alternatives considered

**Runtime feature detection.** Rejected on two counts. It costs a round trip per
action in the hot path, and half of these capabilities cannot be probed without
a side effect — you cannot discover whether your clicks are trusted without
clicking something.

**`NotImplementedError` everywhere.** Rejected: it conflates "this backend never
will" with "nobody wrote it yet", and callers cannot plan around the difference.
It is also indistinguishable from an ordinary bug.

**A capability matrix in docs only.** Rejected: documentation cannot be tested,
and it went stale in the first week of the prototype.

## Consequences

Good: planners route around gaps before committing to a plan; the conformance
suite can enforce honesty rather than only function; a new backend's limits are
legible from one property.

Bad: the `Capability` enum is now itself an interface, and adding a member is a
change every engine author should look at. We accept that, and require an ADR
for any addition.

Ugly: nothing stops a backend from declaring a capability it does not have. The
conformance suite catches it, which is why running the suite is the definition
of a working backend rather than a nice-to-have.

## When we would revisit

If capability declarations start needing to be dynamic — a backend whose
abilities change with the page or the browser version — this design is wrong and
we would move to a probed-and-cached model.
