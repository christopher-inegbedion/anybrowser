# ADR-0002: Every action reports whether anything changed

**Status:** Accepted · **Date:** 2026-08-31

## Context

The most common way a browser agent fails is not a crash. It is a click that
dispatches correctly onto an element covered by an invisible overlay, returns
cleanly, and changes nothing. The engine reports success. History renders
"Result: success". The model reads that as progress and repeats the action until
the run hits its limit — often then reporting the task complete.

Relay measured this on its own harness. Surfacing no-ops truthfully in history
moved a repeat-action benchmark from 38% failure to 6%, with no change to the
planner, the prompt, or the model.

## Decision

`ActionOutcome` carries `ok` *and* `changed`. `ok` is about the call; `changed`
is about the world. An engine must verify rather than assume: read back typed
text, compare scroll positions, compare a cheap structural page signature across
a click.

`ToolResult.changed` carries the same signal into agent history.

Two conformance tests enforce it — clicking dead space and scrolling past the
bottom must both report `changed=False`.

## Alternatives considered

**Let the layer above diff the page.** Rejected: it duplicates work the engine
has already done, and it cannot distinguish "the click did nothing" from "the
click worked and the page happens to look the same", which the engine often can.

**Raise on a no-op.** Rejected: a no-op is not an error. Clicking a disabled
button is a legitimate, informative outcome, and turning it into an exception
pushes control flow into `except` blocks everywhere.

**A confidence score.** Rejected as unfalsifiable. Nobody could say what 0.7
meant, and the conformance suite could not test it.

## Consequences

Good: agents can tell progress from repetition; the suite can fail a dishonest
backend; `detail` gives the layer above something specific to act on.

Bad: engines pay a verification round trip per action. Measured at 30-80ms for
the structural-signature approach, which we judged cheap against a looping run.

Ugly: "changed" is not always well defined. A click that opens a menu which
closes again before we sample it looks like a no-op. We accept false
*negatives* here — an agent told nothing happened tries something else, which is
recoverable; an agent told something happened when it did not is the failure
this ADR exists to prevent.
