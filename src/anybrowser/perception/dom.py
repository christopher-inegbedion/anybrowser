"""DOM perception: the collector's raw output becomes a ``Snapshot``.

The engine owns the round trip; this owns the shape. Handles are minted here
too, so every engine that uses DOM perception agrees on what a handle means and
on when it goes stale.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

from ..core.types import Box, Element, FrameInfo, Snapshot, Viewport
from .assets import load_js, with_helpers

__all__ = ["COLLECT_SCRIPT", "READ_SCRIPT", "build_snapshot", "decode_handle", "encode_handle"]

COLLECT_SCRIPT = with_helpers(load_js("collect-elements"))
READ_SCRIPT = with_helpers(load_js("read"))


def encode_handle(generation: int, index: int) -> str:
    """Mint a handle.

    The generation prefix is what makes staleness detectable without a round
    trip into a page that may no longer exist: it is bumped on every navigation,
    so a handle from the previous page is recognisably from the previous page.
    """
    return f"{generation}:{index}"


def decode_handle(handle: str) -> tuple[int, int]:
    """Split a handle, or raise ``ValueError`` if it was never one of ours."""
    generation, _, index = handle.partition(":")
    if not generation.isdigit() or not index.isdigit():
        raise ValueError(f"malformed handle: {handle!r}")
    return int(generation), int(index)


def _viewport(raw: Mapping[str, Any]) -> Viewport:
    return Viewport(
        width=int(raw.get("width") or 0),
        height=int(raw.get("height") or 0),
        scroll_x=float(raw.get("scrollX") or 0.0),
        scroll_y=float(raw.get("scrollY") or 0.0),
        device_pixel_ratio=float(raw.get("dpr") or 1.0),
    )


def build_snapshot(
    raw: Mapping[str, Any],
    *,
    url: str,
    title: str,
    generation: int,
    frames: Sequence[FrameInfo] = (),
) -> Snapshot:
    """Turn one collector payload into a ``Snapshot``."""
    elements = [
        Element(
            handle=encode_handle(generation, int(item["index"])),
            box=Box(
                float(item.get("x") or 0.0),
                float(item.get("y") or 0.0),
                float(item.get("width") or 0.0),
                float(item.get("height") or 0.0),
            ),
            tag=str(item.get("tag") or ""),
            role=str(item.get("role") or ""),
            label=str(item.get("label") or ""),
            value=str(item.get("value") or ""),
            placeholder=str(item.get("placeholder") or ""),
            editable=bool(item.get("editable")),
            disabled=bool(item.get("disabled")),
            frame_id=str(item.get("frameId") or ""),
            attributes=dict(item.get("attributes") or {}),
        )
        for item in raw.get("elements") or ()
    ]
    return Snapshot(
        url=url,
        title=title,
        viewport=_viewport(raw.get("viewport") or {}),
        elements=elements,
        text=str(raw.get("text") or ""),
        signature=str(raw.get("signature") or ""),
        captured_at=time.time(),
        frames=tuple(frames),
    )
