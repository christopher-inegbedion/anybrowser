# Truthful outcomes

Every action returns two booleans, and the second one is the interesting one:

```python
outcome = await engine.click(target)
outcome.ok  # did the call complete without error
outcome.changed  # did anything actually happen
```

## The failure this prevents

An agent clicks a button. The click dispatches at the right coordinates and
returns cleanly, but the button was covered by an invisible overlay, so nothing
happened. The engine reports success. History renders "Result: success". The
model reads that as progress, sees the page unchanged, and clicks again. And
again, until the run hits its action limit and reports the task complete.

This is the single most common way a browser agent dies, and it is not a model
problem — the model was told the click worked. Measured on Relay's own harness,
surfacing `no_change` truthfully took a repeat-action benchmark from 38% failure
to 6%, with no change to the planner or the prompt.

## The rules

**`ok` is about the call. `changed` is about the world.** A click that dispatched
correctly onto nothing is `ok=True, changed=False`. That is not a failure — there
was no error — but it is not progress either, and the layer above needs to know
which it was.

**Verify, do not assume.** An engine that returns `changed=True` because it sent
the event is lying. Read something back:

- typing → read the field's value; controlled React inputs routinely swallow
  programmatic writes and report nothing wrong
- scrolling → compare scroll position; at the bottom of a page it will not move
- clicking → compare a cheap structural page signature before and after

**Say why in `detail`.** `"already at the scroll limit"` tells the agent to stop
scrolling and try something else. `"no change"` tells it nothing, and it will
guess — usually by repeating the action.

**Put the evidence in `data`.** The layer that renders history for a model needs
specifics: which option got selected, whether the page advanced after an upload,
how many rows an extraction actually returned.

## Where this is enforced

Two conformance tests, and they are the ones most likely to fail a new backend:

- `test_click_on_nothing_reports_no_change` — click dead space, get `changed=False`
- `test_scroll_at_the_bottom_reports_no_change` — scroll past the end, same

Both are trivially passable by an honest engine and impossible for one that
hard-codes `changed=True`.

## The same rule, one layer up

`ToolResult.changed` carries this into agent history for exactly the same
reason. A history that renders every no-op as "success" teaches the model that
repeating the no-op is progress. See
[`agent/tool.py`](../../src/relaykit/agent/tool.py).

See [ADR-0002](../adr/0002-truthful-outcomes.md).
