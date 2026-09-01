# Driving your own Chrome

The `devtools` pipe can only reach a browser started with
`--remote-debugging-port`, and that flag has to be there from launch. By the
time you want to automate the browser you already have open, it is too late.

The `extension` pipe solves that. `chrome.debugger` is a debugger session the
browser hands to an extension, so the extension can attach to the windows and
tabs you already have — with your logins intact.

```python
engine = await open_engine("chrome", mode="extension")
```

The direction is inverted from what you might expect: **the engine listens and
the extension dials in.** A browser extension can only make connections, never
accept them. It also means the extension pipe does not depend on the daemon —
`ChromeEngine(mode="extension")` is self-contained.

## Installing the extension

Chrome is deliberately hostile to loading unpacked extensions, and the failure
is quiet.

**`--load-extension` does not work on Google Chrome.** Chrome stable ignores it
and says so only in its own log:

```
WARNING: --load-extension is not allowed in Google Chrome, ignoring.
```

Nothing in the UI mentions it. If you take that route you will simply find no
extension, with no explanation.

Two things that do work:

### By hand

1. Open `chrome://extensions`
2. Turn on **Developer mode**
3. **Load unpacked** → select `extensions/chrome`
4. Click the extension and set the endpoint if you changed it from
   `ws://127.0.0.1:8787`

### Programmatically, over CDP

`Extensions.loadUnpacked` works where the command-line flag does not. It needs a
browser with a debugging port, so this is for setting up a profile you will then
drive through the extension:

```python
import json, urllib.request, websockets

version = json.load(urllib.request.urlopen("http://127.0.0.1:9222/json/version"))
async with websockets.connect(version["webSocketDebuggerUrl"]) as ws:
    await ws.send(json.dumps({
        "id": 1,
        "method": "Extensions.loadUnpacked",
        "params": {"path": "/absolute/path/to/extensions/chrome"},
    }))
    print(json.loads(await ws.recv()))
```

## What to expect

The extension pipe is a relay through a Manifest V3 service worker, and that has
consequences worth knowing rather than being surprised by:

**It is slower than the DevTools pipe.** Every command is a WebSocket round trip
into a service worker and back. Conformance takes ~2-10s through the extension
against ~2s direct.

**Replies are occasionally lost.** Chrome terminates a Manifest V3 service worker
whenever it likes, most visibly when a navigation tears down the execution
context under an in-flight `Runtime.evaluate`. RelayKit's own page reads are
bounded and retried once, so this shows up as a brief pause rather than a
failure. Your own `evaluate()` calls are *not* retried — they may have side
effects, and running those twice is worse than surfacing an error.

**Only one debugger client per tab.** If DevTools is open on a tab, attaching to
it fails. The engine says so rather than retrying forever.

**The service worker forgets, Chrome does not.** Attachments survive a worker
restart; the worker's memory of them does not. The bridge reconciles the two on
every wake, which is why it can pick up a tab it attached to before being
terminated.

## Capabilities

The extension pipe declares `attach_to_user_session`; the DevTools pipe does
not. That is derived from the live connection rather than stated separately, so
the two cannot disagree:

```python
from relaykit.core import Capability

if Capability.ATTACH_TO_USER_SESSION in engine.capabilities:
    ...  # these are the user's real tabs
```
