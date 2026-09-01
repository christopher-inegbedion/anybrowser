# Porting: the Safari engine

**Tracking:** [#4](https://github.com/christopher-inegbedion/relaykit/issues/4) · **Status:** native half done and verified; perception half outstanding

## What it is

Safari has no CDP. The Web Inspector protocol needs private Apple entitlements,
`chrome.debugger` has no counterpart, and WebDriver BiDi is advertised but
non-functional as of Safari 26.5 (`--bidi` opens no listener; the session returns
`webSocketUrl: true`, a boolean rather than a URL).

`safaridriver` works well and is unusable here for one structural reason: it only
ever drives its own Automation window, a clean profile with none of the user's
logins, and it cannot adopt an existing window. Cookies can be transplanted into
it; a live session cannot. It stays useful for CI fixtures, where a clean profile
is a feature.

So the engine is assembled from five mechanisms, each doing what it is best at:

| Layer | Mechanism | Gives |
|---|---|---|
| Activation | Accessibility (`AXPress`, `AXValue`) | trusted clicks with real user activation, in the background, cursor untouched |
| Perception | Safari Web Extension content script | DOM, coordinate resolution, overlay |
| Gesture | synthetic `PointerEvent`s from that content script | drag, draw, canvas, HTML5 DnD |
| Pixels | `tabs.captureVisibleTab` | screenshots with no Screen Recording grant |
| Navigation | AppleScript | tabs, windows, history |

The routing insight is the transferable part: **driving a browser does not
require a mouse.** Most interaction is better expressed as *activate this
element* than as *click these pixels*, and the activate-form has
background-capable implementations.

## Scope

| Piece | Source | Lands as |
|---|---|---|
| Swift AX helper | `core/safari/mac_engine/` | `engines/safari/mac_engine/` |
| Helper process bridge | `core/safari/engine.py` | `engines/safari/bridge.py` |
| Extension protocol | `core/safari/cdp_client.py` | `engines/safari/extension.py` |
| Safari Web Extension | `extensions/relay/safari/` | `extensions/safari/` |

## Details that are not obvious from the source

**Keep the helper resident.** It holds a warm AX connection and a live
`NSAppleScript` instance: ~5ms per call, against ~80ms for spawning `osascript`.
One JSON object per line over stdio, exactly like the notification helper.

**`AXValue` writes are silently ignored on some controls.** Setting it is honoured
without focus on some elements and dropped on others — returning success either
way. The helper must write, read back, and escalate to focusing only when the
write did not take. Focusing raises Safari, so the result has to say whether that
happened, and `allow_raise=False` must produce a clean failure instead.

**`AXPress` the actionable element, not the deepest one.** The deepest element
under a point is usually an inner `<span>` that cannot be pressed. Walk up to the
nearest pressable ancestor and fail loudly when there is none — never report a
press that did nothing.

**Coordinates convert through the `AXWebArea` origin.** Exact, and it survives
sidebars and page zoom, which arithmetic on the window frame does not.

**The bundle identifier is load-bearing.** A macOS helper app shipped with a
fresh bundle id is refused with no prompt and no error. It must share the host
app's identifier.

## Honest capabilities

Safari declares **no** `CROSS_ORIGIN_FRAMES`, `NETWORK_INTERCEPTION`,
`SCREENCAST` or `INIT_SCRIPTS`. These are not unfinished work — without the
Inspector protocol there is nothing to implement them with. Declaring them
absent is what lets a planner route around them
([ADR-0001](../adr/0001-capabilities-over-exceptions.md)).

## Definition of done

```bash
pytest --pyargs relaykit_conformance --engine safari
```

green on macOS, with the capability-gated tests skipping for the four above,
plus by hand:

- Safari behind another app, `frontmost` verified before and after, cursor
  byte-identical: click a `<button>`, an `<a href>`, a plain `<div>` with a
  handler, and a `div[role=button]` — all trusted, all with user activation
- a screenshot of an occluded window
- a drag on a canvas
