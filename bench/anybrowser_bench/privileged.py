"""What the bench knows at each step that the agent does not.

The agent sees a screenshot. The bench also knows the task's answer, can read
the DOM, and can ask the server what it has received. While collecting
training data -- never while evaluating -- that is recorded beside every step:

- ``goal_reached``: should the agent finish now? For an answer task, the
  answer is on screen, meaning inside the viewport and not merely somewhere in
  the DOM. For a state task, the server already has the right submission. For
  the impossible task, always: it can be answered from the start.
- ``target``: what a coordinate action landed on, read with
  ``elementFromPoint`` just before it ran.

These turn every recorded step into a labelled one. The first two
self-improvement runs learnt the finish decision only from end-of-attempt
outcomes, and swung between finishing too early and never finishing. Here
each screen says what the right call was. No model supplies the labels: they
come from the seed, the DOM and the server, which is why they are trustworthy,
and why they are kept away from the agent's own view.
"""

from __future__ import annotations

import contextvars
from collections.abc import Awaitable, Callable
from typing import Any

from anybrowser.core.engine import BrowserEngine

from .server import BenchServer
from .task import Instance, Kind, _fields_match

__all__ = ["ORACLE", "Oracle", "describe_point", "make_oracle"]

#: (engine) -> facts about the current state. Set per attempt by run_suite.
Oracle = Callable[[BrowserEngine], Awaitable[dict[str, Any]]]
ORACLE: contextvars.ContextVar[Oracle | None] = contextvars.ContextVar("ORACLE", default=None)

# True when ``value`` appears in a text node that intersects the viewport.
# Visible on screen, not merely present: long-list's price is in the DOM from
# the start and on screen only after scrolling, and an image-only agent can
# only be told to finish once it could have seen it.
_VISIBLE_JS = """(value) => {
  const norm = (s) => s.replace(/\\s+/g, ' ').trim();
  const want = norm(value);
  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = walk.nextNode(); n; n = walk.nextNode()) {
    if (!norm(n.textContent || '').includes(want)) continue;
    const range = document.createRange();
    range.selectNodeContents(n);
    for (const r of range.getClientRects()) {
      if (r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0 &&
          r.top < innerHeight && r.left < innerWidth) return true;
    }
  }
  return false;
}"""

# What a point would hit: the element's tag and the text a person would call
# it by -- a link's words, a field's label or placeholder, a button's caption.
_DESCRIBE_JS = """(x, y) => {
  const el = document.elementFromPoint(x, y);
  if (!el) return null;
  const tag = el.tagName.toLowerCase();
  let text = '';
  if (tag === 'input' || tag === 'select' || tag === 'textarea') {
    const label = el.labels && el.labels[0] ? el.labels[0].innerText : '';
    text = label || el.getAttribute('placeholder') || el.getAttribute('name') || '';
  } else {
    text = el.innerText || el.getAttribute('aria-label') || '';
  }
  text = text.replace(/\\s+/g, ' ').trim().slice(0, 60);
  return {tag, text, canvas: tag === 'canvas'};
}"""


def make_oracle(instance: Instance, server: BenchServer, root: str) -> Oracle:
    async def oracle(engine: BrowserEngine) -> dict[str, Any]:
        if instance.kind is Kind.IMPOSSIBLE:
            return {"goal_reached": True}
        if instance.kind is Kind.STATE:
            received = [s for s in server.submissions(root) if s.path == instance.expected_path]
            reached = bool(received) and _fields_match(
                received[-1].fields, instance.expected_fields
            )
            return {"goal_reached": reached}
        if instance.answer_visibility == "always":
            return {"goal_reached": True}
        try:
            visible = bool(await engine.evaluate(_VISIBLE_JS, instance.expected))
        except Exception:
            return {"goal_reached": None}  # unknown: such a step gets no label
        return {"goal_reached": visible}

    return oracle


async def describe_point(engine: BrowserEngine, x: Any, y: Any) -> dict[str, Any] | None:
    try:
        result = await engine.evaluate(_DESCRIBE_JS, float(x), float(y))
    except Exception:
        return None
    return result if isinstance(result, dict) else None
