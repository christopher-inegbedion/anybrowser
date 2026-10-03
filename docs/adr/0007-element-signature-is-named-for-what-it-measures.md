# ADR-0007: The element fingerprint is named for what it measures

**Status:** Accepted · **Date:** 2026-10-03

## Context

[ADR-0006](0006-waiting-is-an-engine-primitive.md) left a known wart in the
interface. There are two page fingerprints, and they had names that did not tell
them apart:

- `Snapshot.signature`, derived from the element set by `collect-elements.js`
- `BrowserEngine.page_signature()`, the URL, the page text and the focused field

They are not interchangeable. Changing a paragraph's text moves the second and
not the first, so a "did anything happen" check built on `Snapshot.signature`
misses a result arriving as text, which is the most common thing worth noticing.
ADR-0006 put a warning in a docstring and deferred the rename, because renaming
either one is a breaking change.

A docstring warning is the wrong defence. The person who reaches for the wrong
one is a third-party engine author with a `Snapshot` already in hand, and
`.signature` is the obvious attribute to compare. Nothing at that point tells
them otherwise. The project has also just shipped a second bare `signature`:
`ToolContext.signature` in 0.3.0 holds a *page* signature, so the bare word now
meant both things.

Nothing in this repository reads `Snapshot.signature`. Engines set it, and the
daemon carries it across the wire. So almost all of the cost of renaming falls
on code outside this repository, and the deprecation path below is for those
authors.

## Decision

**`Snapshot.signature` becomes `Snapshot.element_signature`.** The method keeps
its name: `page_signature()` already says what it measures, and it is the one
that answers the question people mean to ask.

**`ToolContext.signature` becomes `ToolContext.page_signature`**, so the bare
word is gone from the public surface entirely.

**The old `Snapshot` name keeps working, with a warning, until 1.0.** Both
reading `snapshot.signature` and constructing with `Snapshot(signature=...)`
work, and both raise a `DeprecationWarning` that names `page_signature()`. A
silent alias would keep the confusion; the point of the warning is to tell the
author which fingerprint they probably meant. Passing both names is a
`TypeError` rather than a guess.

**The daemon sends both keys and reads either.** A 0.3 peer on either end of the
connection keeps the field, so this is not a `PROTOCOL_VERSION` bump. Removing
the old key is the breaking change that the next bump will announce.

The page scripts still emit `signature` as an internal key, and the Python side
maps it once. Renaming that key means rebuilding the Safari extension, which is
a cost with no effect on users.

## Consequences

Good: the name now says what the field measures, at the moment someone reaches
for it. An engine written against 0.3 keeps running and is told what changed and
why.

Bad: a shim around a frozen dataclass's generated `__init__`, which is less
obvious than a plain field, and two keys on the wire until the next protocol
bump.

`ToolContext.signature` gets no shim. It shipped in 0.3.0, has one reader (the
`wait` tool), and a deprecation path for it would outlive any code that used it.

## When we would revisit

At 1.0 the shim and the `signature` wire key are removed. If a third fingerprint
ever appears, such as pixels or the accessibility tree, each one is named for
what it measures. A bare `signature` is not used again.
