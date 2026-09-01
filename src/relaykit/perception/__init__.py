"""Turning a live page into a Snapshot. Engine-agnostic.

An engine supplies a way to evaluate script (or to read an accessibility tree);
this package turns what comes back into
:class:`~relaykit.core.types.Snapshot`. Keeping it out of the engines means
Chrome, Safari and a third-party backend see the same page the same way, and a
perception improvement lands for all of them at once.
"""

from .assets import DEEP_DOM_HELPERS, load_js, with_helpers
from .dom import COLLECT_SCRIPT, READ_SCRIPT, build_snapshot

__all__ = [
    "COLLECT_SCRIPT",
    "DEEP_DOM_HELPERS",
    "READ_SCRIPT",
    "build_snapshot",
    "load_js",
    "with_helpers",
]
