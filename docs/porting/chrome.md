# Porting: the Chrome engine

**Tracking:** [#1](https://github.com/relaykit/relaykit/issues/1) · **Status:** in progress

## What it is

CDP, but the debugger session belongs to a browser extension rather than to a
launched-with-`--remote-debugging-port` process. That is the whole reason this
engine exists: it attaches to the browser the user already has open, with their
tabs and their logins, which no launcher-based automation can do.

```
engine  ──JSON over WS──▶  daemon  ──chrome.debugger──▶  extension  ──▶  the page
```

Declared capabilities: everything except nothing — this is the most capable
backend, and the reference for what the others are measured against. See
`PLANNED_CAPABILITIES` in `src/relaykit/engines/chrome/__init__.py`.

## Scope

| Piece | Source | Lands as |
|---|---|---|
| CDP client, session multiplexing | `core/daemon/cdp_client.py` | `engines/chrome/cdp.py` |
| Tab attach/detach, ownership | `core/daemon/cdp_tab_manager.py` | `engines/chrome/tabs.py` |
| Page operations | `core/daemon/page_facade.py` | folded into `engine.py` |
| DOM perception | `browser/dom.py`, `browser/js/*.js` | `perception/dom.py` |
| Screenshot, full-page tiling | `page_facade.screenshot` | `engines/chrome/capture.py` |

The `PageFacade` does **not** come across as-is. It exists to present a
Playwright `Page` surface over CDP ([ADR-0003](../adr/0003-narrow-engine-interface.md));
against the narrow interface most of it is unnecessary. Take the CDP call
sequences out of it and leave the facade behind.

## Details that are not obvious from the source

Each of these cost real debugging time. They are the reason this file exists.

**Drag moves need `buttons: 1`.** `Input.dispatchMouseEvent` with
`type: mouseMoved` defaults to no pressed buttons. Without it, HTML5
drag-and-drop is 0-for-8 and range inputs step exactly once, while every call
returns success. Measured: 45% of drags silently failing at a reported 100%
success rate. Conformance covers it — `test_drag_carries_the_pressed_button`.

**Out-of-process iframes need their own `sessionId`.** Evaluating in a
cross-origin frame from the page session silently targets the wrong context, so
field typing is never verified and the agent scroll-loops looking for the form it
just filled. Track child sessions from `Target.attachedToTarget` and route by
`sessionId`.

**Exempt file inputs from the viewport filter.** `<input type="file">` is
`display: none` on essentially every real upload flow. Keep un-anchored ones with
a sentinel box rather than dropping them, or uploads are impossible on the sites
people actually automate.

**Screenshots must come from the screencast buffer, not `fromSurface: true`.**
Capturing from the surface cannot photograph a frame the compositor has not
presented, and any overlay hidden per-poll on the live surface produces a visible
blink between iterations.

**Full-page capture is tiled, not one call.** Beyond a few thousand pixels
`captureBeyondViewport` fails or truncates. Tile, stitch, and note in
`Capabilities.notes` that fixed headers repeat — do not silently return a
viewport shot.

## Definition of done

```bash
pytest --pyargs relaykit_conformance --engine chrome
```

green, plus by hand — because the suite runs against a local fixture server and
cannot cover these:

- attaches to an already-open browser with existing logins intact
- typing into a cross-origin iframe form, verified in that frame's context
- upload through a hidden file input on a real ATS form
- a drag on a page that reads `event.buttons`
