"""Compatibility entry point for ``python -m anybrowser.cli.main``."""

from __future__ import annotations

from .app import build_parser, main

__all__ = ["build_parser", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
