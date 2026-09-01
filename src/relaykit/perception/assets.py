"""Loading the JavaScript that runs in the page.

The scripts live as ``.js`` files rather than as Python string literals so they
are lintable, diffable and editable with syntax highlighting. They are read at
import time and cached.

They must also be declared as package data. Omitting them builds a wheel that
imports perfectly and raises ``FileNotFoundError`` on first real use -- a
failure invisible in the source tree and in every source-tree test. CI installs
the built wheel and exercises it for exactly this reason.
"""

from __future__ import annotations

import functools
from pathlib import Path

__all__ = ["DEEP_DOM_HELPERS", "load_js", "with_helpers"]

_JS_DIR = Path(__file__).parent / "js"

#: Placeholder a script uses to pull in the shared helpers.
_HELPERS_TOKEN = "__RELAYKIT_DEEP_DOM_HELPERS__"


@functools.cache
def load_js(name: str) -> str:
    """Read ``js/<name>.js``. Raises if it is missing rather than degrading."""
    path = _JS_DIR / f"{name}.js"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"page script {name!r} is missing from the install. If this is an "
            f"installed copy rather than a checkout, the wheel omitted its "
            f"package data. Looked in {_JS_DIR}."
        ) from exc


DEEP_DOM_HELPERS = load_js("deep-dom-helpers")


def with_helpers(script: str) -> str:
    """Inline the deep-DOM helpers into a script that asks for them."""
    return script.replace(_HELPERS_TOKEN, DEEP_DOM_HELPERS)
