# ADR-0004: The engine interface is async

**Status:** Accepted · **Date:** 2026-08-31

## Context

Every real backend is asynchronous underneath. CDP is a WebSocket. The Safari
engine is a line-oriented subprocess holding a warm accessibility connection.
WebDriver is HTTP. Meanwhile a lot of the code people write on top — scripts,
notebooks, an executor ported from a synchronous codebase — wants to block.

Someone has to hide an event loop. The question is who.

## Decision

`BrowserEngine` is async. `SyncEngine` wraps any engine in a blocking facade
that owns exactly one background loop.

## Alternatives considered

**A synchronous interface, each backend hiding its own loop.** Rejected: the
loop-hiding is then duplicated per backend, and every backend author gets to
reinvent `run_coroutine_threadsafe` and its deadlocks. Relay did this and paid
for it in a class of bug that only reproduced under concurrent tab operations.

**Both interfaces, generated.** Rejected as a maintenance trap: two surfaces
drift, and the generated one is always the one with the worse stack traces.

**Sync-first with an async adapter.** Rejected: adapting sync to async requires
a thread pool and gives up cancellation, which is what makes a run stoppable
mid-action.

## Consequences

Good: backends are written the way their protocol actually works; cancellation
works, so a run can be stopped or steered mid-action; concurrency inside the
daemon is expressible.

Bad: `async` is contagious for callers who did not want it. `SyncEngine` is the
answer and it is one extra concept to learn.

Ugly: `SyncEngine` proxies by attribute lookup, so a typo becomes an
`AttributeError` at call time rather than a type error at write time. Static
type checkers see through it poorly. Acceptable for a compatibility shim; it
would not be acceptable for the primary interface.
