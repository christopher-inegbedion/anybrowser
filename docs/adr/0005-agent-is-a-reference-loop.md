# ADR-0005: The agent runtime is a reference loop, not the product

**Status:** Accepted · **Date:** 2026-10-02

## Context

This library's load-bearing parts are the three plugin interfaces and the
contracts that grade them: `BrowserEngine`, `DaemonTransport`, `ModelProvider`.
That is what `pytest --pyargs anybrowser_conformance --engine yours` exists for,
and it is why a third party can add a backend and know when it is finished.

`anybrowser.agent` is not one of those interfaces. It was written to prove the
three compose into something that runs, and its own docstring says so:

> Deliberately small. Everything that decides *quality* lives in the planner and
> in perception; this file's whole job is to be an honest, interruptible loop
> that never lies to the planner about what happened.

That intent was never written down as a decision, and the surface drifted
towards being a product one tool at a time. The drift had a shape worth
recording, because it was not sloppiness so much as the absence of a rule:

- A `look` tool appeared, which re-fetched the snapshot the loop had already
  taken for that step. It cost a step, ticked the repetition breaker, and
  returned nothing new. Removed in 05cb2ab.
- The prompt never rendered `Snapshot.text`, which the loop had been collecting
  all along, so an agent could see what was clickable and never what a page
  said. That read as a missing *capability* and was nearly answered by adding
  one. It was a renderer discarding data.
- Thirteen engine capabilities — `hover`, `go_back`, `reload`, the tab trio,
  `handle_dialog`, `set_zoom`, `drag`, `evaluate`, cookies — have no tool, so
  the conformance suite grades engines on things the loop cannot reach.

Each was treated as a defect. Whether they are defects depends entirely on what
the agent layer is *for*, and nobody had said.

## Decision

**The agent runtime is a reference loop.** It exists to demonstrate the
interfaces fitting together and to be the smallest honest example of a browsing
agent. It is customisable and extensible, and it is not where this library's
value lives.

Two consequences, and the second is the useful one:

**A small tool surface is correct, not incomplete.** An absent tool is not a
bug. Anyone who needs `hover`, tabs or dialogs calls the engine, which already
implements them and is graded on them; or registers their own `Tool`; or brings
their own loop entirely. The README says to bring your own agent, rather than
leaving that to be discovered mid-task.

**Capabilities belong to the layers that are contract-graded, never to the
loop.** A capability added to `AgentRunner` is available only to
`AgentRunner`. The same capability on `BrowserEngine` is available to every
loop, must be provided by every backend, and is enforced by the suite. So when
something is missing, the question is which layer it belongs to — and the answer
is almost never this one.

## What the loop still guarantees

Being thin is not licence to be dishonest. The loop keeps exactly the
properties that make a run debuggable, and they are tested:

- it re-observes before every decision, so no planner reasons about a stale page
- it records no-ops as no-ops ([ADR-0002](0002-truthful-outcomes.md)), and a
  read is not marked as a failed write
- it stops: a budget, a stop signal, and a repetition breaker that counts any
  unchanged step whatever shape it took
- every decision carries a human-readable narrative, required at construction

## Consequences

Good: the surface stops growing by accident, and "which layer?" has an answer.
Capabilities land where they are graded, which is where they help most people.

Bad: someone arriving expecting a finished agent will find a sparse one. The
README has to say so plainly, and an issue asking for a tool is a scope question
rather than a bug report.

Ugly: the line between "the loop being honest" and "the loop being good" is not
always obvious. `await_change` — waiting for an in-flight result to land — reads
like honesty, and it is proposed as an *engine* primitive for that reason. The
test is whether a capability belongs to browsers in general or to this loop in
particular.

## When we would revisit

If the reference loop becomes what people actually deploy, this is the wrong
shape and the honest response is to promote `Planner` to a fourth plugin
interface with a contract of its own — so third-party loops are graded like
third-party engines — rather than to quietly grow this one.
