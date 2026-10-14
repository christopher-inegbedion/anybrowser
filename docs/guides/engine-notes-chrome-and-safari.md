# Engine notes: what the Chrome and Safari engines learned

Details from building the two reference engines against real user browsers. They
are invisible in the source and each cost real debugging time, so they are kept
here for anyone writing an engine. Start with [Writing an engine](writing-an-engine.md);
the hidden-file-input and `buttons: 1` drag lessons live there because they catch
everyone, and are not repeated.

## Chrome (CDP)

The Chrome engine is written once over two pipes
([`cdp.py`](../../src/anybrowser/engines/chrome/cdp.py)). `devtools` is a direct
WebSocket to a browser started with `--remote-debugging-port`; it is the only mode
CI can run, because it can launch its own browser. `extension` relays CDP through
a browser extension holding a `chrome.debugger` session, which is how it attaches
to the browser the user already has open, with their tabs and logins. The
connection reports whether it reaches a real user session, and the declared
`ATTACH_TO_USER_SESSION` capability is derived from that, so the two cannot
disagree. See [driving your own Chrome](chrome-extension.md).

**Out-of-process iframes need their own `sessionId`.** Evaluating in a
cross-origin frame from the page session silently targets the wrong context, so
field typing is never verified and the agent scroll-loops looking for the form it
just filled. Track child sessions from `Target.attachedToTarget` and route by
`sessionId`.

**Screenshots must come from the screencast buffer, not `fromSurface: true`.**
Capturing from the surface cannot photograph a frame the compositor has not
presented, and any overlay hidden per-poll on the live surface produces a visible
blink between iterations.

**Full-page capture is tiled, not one call.** Beyond a few thousand pixels
`captureBeyondViewport` fails or truncates. Tile, stitch, and note in
`Capabilities.notes` that fixed headers repeat. Do not silently return a viewport
shot.

**`Input.dispatchMouseEvent` defaults to no pressed buttons.** This is the root of
the `buttons: 1` drag bug in the guide: range inputs step exactly once and HTML5
drag-and-drop is 0-for-8, while every call returns success.
`test_drag_carries_the_pressed_button` covers it.

**`<input type="file">` is `display: none` on essentially every real upload
flow.** Keep un-anchored ones with a sentinel box rather than dropping them.

### Checks the conformance suite cannot make

The suite runs against a local fixture server, so verify these by hand:

- attaches to an already-open browser with existing logins intact
- typing into a cross-origin iframe form, verified in that frame's context
- upload through a hidden file input on a real ATS form
- a drag on a page that reads `event.buttons`

## Safari (no CDP)

Safari has no CDP. The Web Inspector protocol needs private Apple entitlements,
`chrome.debugger` has no counterpart, and WebDriver BiDi is advertised but
non-functional as of Safari 26.5 (`--bidi` opens no listener; the session returns
`webSocketUrl: true`, a boolean rather than a URL).

`safaridriver` works well and is unusable for driving a user's browser for one
structural reason: it only ever drives its own Automation window, a clean profile
with none of the user's logins, and it cannot adopt an existing window. Cookies
can be transplanted into it; a live session cannot. It stays useful for CI
fixtures, where a clean profile is a feature.

So the engine is assembled from five mechanisms, each doing what it is best at:

| Layer | Mechanism | Gives |
|---|---|---|
| Activation | Accessibility (`AXPress`, `AXValue`) | trusted clicks with real user activation, in the background, cursor untouched |
| Perception | Safari Web Extension content script | DOM, coordinate resolution, overlay |
| Gesture | synthetic `PointerEvent`s from that content script | drag, draw, canvas, HTML5 DnD |
| Pixels | `tabs.captureVisibleTab` | screenshots with no Screen Recording grant |
| Navigation | AppleScript | tabs, windows, history |

The transferable idea: **driving a browser does not require a mouse.** Most
interaction is better expressed as *activate this element* than as *click these
pixels*, and the activate-form has background-capable implementations. See
[driving Safari](safari-extension.md).

**Keep the helper resident.** It holds a warm AX connection and a live
`NSAppleScript` instance: ~5ms per call, against ~80ms for spawning `osascript`.
One JSON object per line over stdio.

**`AXValue` writes are silently ignored on some controls.** Setting it is honoured
without focus on some elements and dropped on others, returning success either
way. Write, read back, and escalate to focusing only when the write did not take.
Focusing raises Safari, so the result has to say whether that happened, and
`allow_raise=False` must produce a clean failure instead.

**`AXPress` the actionable element, not the deepest one.** The deepest element
under a point is usually an inner `<span>` that cannot be pressed. Walk up to the
nearest pressable ancestor and fail loudly when there is none. Never report a
press that did nothing.

**Coordinates convert through the `AXWebArea` origin.** Exact, and it survives
sidebars and page zoom, which arithmetic on the window frame does not.

**The bundle identifier is load-bearing.** A macOS helper app shipped with a fresh
bundle id is refused with no prompt and no error. It must share the host app's
identifier.

**Declare absences.** Safari declares no `CROSS_ORIGIN_FRAMES`,
`NETWORK_INTERCEPTION`, `SCREENCAST` or `INIT_SCRIPTS`. These are not unfinished
work: without the Inspector protocol there is nothing to implement them with.
Declaring them absent is what lets a planner route around them
([ADR-0001](../adr/0001-capabilities-over-exceptions.md)).

### Checks the conformance suite cannot make

- Safari behind another app, `frontmost` verified before and after, cursor
  byte-identical: click a `<button>`, an `<a href>`, a plain `<div>` with a
  handler, and a `div[role=button]`, all trusted, all with user activation
- a screenshot of an occluded window
- a drag on a canvas
