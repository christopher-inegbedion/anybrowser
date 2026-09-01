"""Import every entry point this package declares.

Twice now a defect has hidden in packaging metadata rather than in code: a
console script pointing at an unwritten module, and two model providers that
never existed. Both are invisible to the test suite -- nothing imports a plugin
by its declared path -- and both surface on someone's first install.

Run against an installed copy, where the metadata is real:

    python scripts/check_entry_points.py
"""

from __future__ import annotations

import importlib
import sys
from importlib.metadata import entry_points

GROUPS = ("relaykit.engines", "relaykit.transports", "relaykit.models", "console_scripts")


def main() -> int:
    failures: list[str] = []
    checked = 0

    for group in GROUPS:
        for ep in entry_points(group=group):
            # console_scripts is a shared namespace; only ours is our problem.
            if group == "console_scripts" and not ep.value.startswith("relaykit"):
                continue
            checked += 1
            module, _, attr = ep.value.partition(":")
            try:
                loaded = importlib.import_module(module)
            except Exception as exc:
                failures.append(f"{group}/{ep.name}: cannot import {module}: {exc}")
                continue
            if attr and not hasattr(loaded, attr.split(".")[0]):
                failures.append(f"{group}/{ep.name}: {module} has no attribute {attr}")

    for failure in failures:
        print(f"BROKEN  {failure}", file=sys.stderr)
    print(f"checked {checked} entry point(s), {len(failures)} broken")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
