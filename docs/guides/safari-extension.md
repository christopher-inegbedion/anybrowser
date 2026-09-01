# Driving Safari

Safari has no CDP. The Web Inspector protocol is gated behind private Apple
entitlements, and `safaridriver` only ever drives its own Automation window — a
clean profile with none of your logins, which it cannot be made to adopt.

So the Safari engine is assembled from two halves, each doing what only it can:

| Half | Mechanism | Owns |
|---|---|---|
| **Native** | Swift helper, Accessibility + ScreenCaptureKit | trusted clicks, field writes, screenshots of occluded windows, native dialogs |
| **Extension** | Safari Web Extension | the DOM, element geometry, synthetic pointer gestures |

The split is not arbitrary. A synthetic click carries no user activation, so
anything gated on it — `window.open`, clipboard, media playback, file pickers —
silently does nothing while reporting success. `AXPress` produces a *trusted*
event, in the background, without moving the cursor. Conversely there is no
accessibility verb for "drag along this path", and pages implement drag with
pointer events anyway, so gestures belong in the extension.

## Setup

Three steps, and the first one has a trap.

### 1. Build the native helper — with **your** bundle identifier

```python
from relaykit.engines.safari import build_engine
build_engine("~/.relaykit", bundle_id="com.example.yourapp", app_name="Your App")
```

`bundle_id` must be the identifier of the application that ships the helper, not
a new one. macOS keys its permission database on code identity, so the
Accessibility grant the user gave your app covers this helper **only if the
identifiers match**. A fresh identifier produces a second entry in Privacy &
Security that the user has to find and approve separately — and is frequently
refused with no prompt at all, which presents as an engine that silently cannot
click anything.

Grant Accessibility to that application once, in System Settings ▸ Privacy &
Security ▸ Accessibility. The engine refuses to start without it and says so.

### 2. Build and install the extension

```bash
python scripts/build_safari_extension.py --convert build/
```

That assembles the extension, converts it to a macOS app with Apple's
`safari-web-extension-converter`, and builds it. Then open the produced
`.app` once and enable it in Safari ▸ Settings ▸ Extensions, granting it access
to the sites you want automated. There is no API to enable an extension; only a
person can.

### 3. Run

```python
engine = await open_engine("safari")
```

## What Safari cannot do

These are declared absent, not unimplemented — there is nothing to build them
with, so a planner routes around them instead of discovering it mid-task:

- `cross_origin_frames` — reachable only through the extension, per frame
- `network_interception` — no debugger protocol
- `screencast` — likewise
- `init_scripts` — nothing runs before page scripts on every navigation

## Things that cost a build each

**The perception JavaScript is not duplicated.** `scripts/build_safari_extension.py`
copies it from `src/relaykit/perception/js/`, which is the same source the Chrome
engine evaluates over CDP. That is what keeps both browsers seeing a page
identically; there is no second copy to drift.

**A background page, not a service worker.** Safari terminates a Manifest V3
service worker before async callbacks resolve, which kills the socket to the
engine mid-conversation. Safari also rejects `"persistent": true` as an unknown
key — the background *page* is the part that matters, and Safari honours that.

**Return a Promise from `onMessage`.** The Chrome idiom, `return true` plus
`sendResponse`, does not work: Safari treats any non-`undefined` return as the
answer, so with several listeners the first to return anything settles the
caller, usually with `undefined`.

**Bundle identifiers must nest.** `safari-web-extension-converter` derives the
*app's* identifier from the app name while taking the *extension's* from
`--bundle-identifier`. Left alone they do not nest and Xcode refuses with
"Embedded binary's bundle identifier is not prefixed with the parent app's". The
build script overrides the app id to match.
