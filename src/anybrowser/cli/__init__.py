"""The ``anybrowser`` command line."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .app import build_parser, main

__all__ = ["build_parser", "main"]


def __getattr__(name: str) -> Any:
    # Resolved lazily so that `python -m anybrowser.cli.app` does not find its
    # own module already imported by this package, which runpy warns about.
    if name in __all__:
        from . import app

        return getattr(app, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
