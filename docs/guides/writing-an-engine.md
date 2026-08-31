# Writing an engine

A backend for a browser RelayKit doesn't support. Budget a day for something
that works and a week for something that passes conformance cleanly — the gap
between those two is where all the interesting bugs live.

## 1. Scaffold

```python
from relaykit.core.engine import BrowserEngine, Capabilities, Capability, EngineInfo
from relaykit.core.errors import EngineNotAvailable
from relaykit.core.types import ActionOutcome, Snapshot, Viewport


class FirefoxEngine(BrowserEngine):
    name = "firefox"  # must match the entry-point key

    @classmethod
    async def probe(cls) -> None:
        """Cheap, side-effect free. Do not launch a browser here."""
        if not shutil.which("firefox"):
            raise EngineNotAvailable("firefox is not installed")

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities.of(
            Capability.EVALUATE_JS,
            Capability.TRUSTED_INPUT,
        )
```

Implement the abstract methods; leave everything else alone. The unimplemented
optional methods already raise `CapabilityNotSupported` with a sensible message,
which is exactly right for a capability you did not declare.

## 2. Register it

In your own package — you do not need to fork RelayKit:

```toml
[project.entry-points."relaykit.engines"]
firefox = "my_package.engine:FirefoxEngine"
```

```python
from relaykit import open_engine

engine = await open_engine("firefox")
```

For tests, skip packaging entirely:

```python
from relaykit.core import engines

engines.register("firefox", FirefoxEngine)
```

## 3. Run the contract

```bash
pytest --pyargs relaykit_conformance --engine firefox
pytest --pyargs relaykit_conformance --engine firefox --engine-option headless=false
```

Green is the definition of done. Until then, work down the list — the tests are
ordered roughly by how much everything else depends on them.

## The five things that catch everyone

### Handles must survive until navigation, and not past it

`Element.handle` is opaque and yours. Whatever it is — a CDP backend node id, an
index into a registry you stash in the page, an accessibility ref — it must
address the same element on the next call, and it must raise `StaleHandle`
(not `ElementNotFound`) once the page has moved. Callers respond differently:
`StaleHandle` means re-snapshot and retry, `ElementNotFound` means give up on
that element.

The reference engine tags handles with a generation counter bumped on every
navigation, which makes staleness detectable without a round trip into a page
that may no longer exist.

### Boxes are CSS pixels, not device pixels

On a Retina display, forgetting to divide by the device pixel ratio gives boxes
twice the size, clicks land at double the coordinates, and everything works
perfectly in a screenshot while hitting nothing. `test_snapshot_boxes_are_viewport_pixels`
catches the obvious version of this; the subtle version is elements inside a
zoomed page or an iframe, which the test cannot catch and you should check by
hand.

### Keep hidden file inputs

Every visibility filter you write will exclude `<input type="file">`, because on
every real site it is `display: none` behind a styled button. Exempt it
explicitly. Without it, uploads are impossible on LinkedIn, Greenhouse, Workday
and roughly everything else people actually automate.

### Drag moves must carry the pressed-button state

If your move events don't set `buttons=1`, drag will look perfect from your side
and move nothing on any page that checks `event.buttons` — which is most of
them, and all HTML5 drag-and-drop. Relay shipped this bug: 45% of drags silently
failed while reporting 100% success. `test_drag_carries_the_pressed_button`
exists because of it.

### Read back before claiming a change

See [truthful outcomes](../architecture/truthful-outcomes.md). Typing must verify
the text landed; scrolling must compare positions; clicking needs some signal
that the page moved. An engine that returns `changed=True` unconditionally passes
almost every test in the suite and makes every agent on top of it loop forever.

## What "good" looks like beyond green

The suite tests the contract, not the quality. Two engines can both pass and one
of them is far better to build on:

- **Snapshot recall.** Does it find the element a person would click? Deeply
  nested custom components, shadow DOM, elements only reachable by scrolling a
  container rather than the page.
- **Cross-origin frames.** Most real forms have one. If you declare
  `CROSS_ORIGIN_FRAMES`, typing into an OOPIF has to be verified in that frame's
  own context, not the main one.
- **Scroll containers.** Pages that scroll a `div` rather than the document.
  Scrolling the window does nothing and an agent will conclude the page has
  ended.

## Getting it listed

Open an issue with your conformance output and we'll link your package from the
README. Engines live *in* this repo only if we commit to maintaining them.
