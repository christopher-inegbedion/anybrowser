# Capabilities, not exceptions

An engine declares what it can do before you ask it to do anything:

```python
if Capability.POINTER_GESTURES in engine.capabilities:
    await engine.drag(path)
else:
    await fall_back_to_clicks()
```

rather than

```python
try:
    await engine.drag(path)
except NotImplementedError:
    await fall_back_to_clicks()  # after the run already went wrong
```

## Why

Browser backends differ **in kind**, not in quality, and the differences are
structural rather than incidental:

- **Safari has no CDP.** The Web Inspector protocol requires private Apple
  entitlements. There is no `chrome.debugger` equivalent, and WebDriver BiDi is
  advertised but non-functional. Network interception and pre-navigation init
  scripts are not "not implemented yet" — they are not reachable.
- **WebDriver cannot adopt your window.** `safaridriver` only ever drives its own
  Automation profile: clean slate, none of your logins. Cookies can be
  transplanted; a live session cannot.
- **A content script cannot produce trusted input.** Synthetic events carry no
  user activation, so anything gated on activation — `window.open`, clipboard,
  media playback, file pickers — silently does nothing.

A planner that knows the browser cannot intercept network requests plans a
different route. A planner that finds out by catching an exception has already
committed to the wrong one, three actions in.

## The rules

**Declare only what you have.** Under-claiming skips conformance tests.
Over-claiming fails them. Both are acceptable; only one is dishonest.

**`CapabilityNotSupported` means never, not now.** It is for "no implementation
of me will ever do this". A transient failure is `ActionFailed`. Callers branch
on the first and retry the second, so confusing them turns a retryable blip into
an abandoned task and a permanent gap into an infinite retry loop.

**Partial support goes in `notes`, not in a silent half-yes.**

```python
Capabilities.of(
    Capability.FULL_PAGE_SCREENSHOT,
    full_page_screenshot="stitched from viewport tiles; fixed headers repeat",
)
```

The note reaches diagnostics and bug reports. A backend that quietly returns a
viewport screenshot for a full-page request is worse than one that refuses.

## Why not just feature-detect at runtime

We tried. Two problems. Probing costs a round trip on every action in the hot
path, and half of these cannot be probed without a side effect — you cannot find
out whether your clicks are trusted without clicking something. Declaration is
cheap, static, and testable, which is why the conformance suite can enforce it.

See [ADR-0001](../adr/0001-capabilities-over-exceptions.md).
