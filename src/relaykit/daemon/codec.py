"""Turning value types into JSON and back.

The wire carries plain JSON, and the types in :mod:`relaykit.core.types` are
frozen dataclasses. This is the one place that knows how to cross that line, so
the server and the client can never disagree about the shape of a ``Snapshot``.

Bytes are the only awkward part: screenshots are binary and JSON is not, so they
travel base64-encoded under an explicit key rather than being smuggled through a
string field that would silently corrupt on a non-UTF-8 byte.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any

from ..core.types import (
    ActionOutcome,
    Box,
    Element,
    FrameInfo,
    NavigationResult,
    Point,
    Screenshot,
    Snapshot,
    TabInfo,
    Viewport,
)

__all__ = [
    "dump_box",
    "dump_element",
    "dump_navigation",
    "dump_outcome",
    "dump_screenshot",
    "dump_snapshot",
    "dump_tab",
    "dump_viewport",
    "load_box",
    "load_element",
    "load_navigation",
    "load_outcome",
    "load_point_or_element",
    "load_screenshot",
    "load_snapshot",
    "load_tab",
    "load_viewport",
]


def dump_box(box: Box) -> dict[str, float]:
    return {"x": box.x, "y": box.y, "width": box.width, "height": box.height}


def load_box(raw: Mapping[str, Any]) -> Box:
    return Box(
        float(raw.get("x", 0.0)),
        float(raw.get("y", 0.0)),
        float(raw.get("width", 0.0)),
        float(raw.get("height", 0.0)),
    )


def dump_viewport(viewport: Viewport) -> dict[str, Any]:
    return {
        "width": viewport.width,
        "height": viewport.height,
        "scroll_x": viewport.scroll_x,
        "scroll_y": viewport.scroll_y,
        "device_pixel_ratio": viewport.device_pixel_ratio,
        "page_zoom": viewport.page_zoom,
    }


def load_viewport(raw: Mapping[str, Any]) -> Viewport:
    return Viewport(
        width=int(raw.get("width", 0)),
        height=int(raw.get("height", 0)),
        scroll_x=float(raw.get("scroll_x", 0.0)),
        scroll_y=float(raw.get("scroll_y", 0.0)),
        device_pixel_ratio=float(raw.get("device_pixel_ratio", 1.0)),
        page_zoom=float(raw.get("page_zoom", 1.0)),
    )


def dump_element(element: Element) -> dict[str, Any]:
    return {
        "handle": element.handle,
        "box": dump_box(element.box),
        "tag": element.tag,
        "role": element.role,
        "label": element.label,
        "value": element.value,
        "placeholder": element.placeholder,
        "editable": element.editable,
        "disabled": element.disabled,
        "frame_id": element.frame_id,
        "attributes": dict(element.attributes),
    }


def load_element(raw: Mapping[str, Any]) -> Element:
    return Element(
        handle=str(raw.get("handle", "")),
        box=load_box(raw.get("box") or {}),
        tag=str(raw.get("tag", "")),
        role=str(raw.get("role", "")),
        label=str(raw.get("label", "")),
        value=str(raw.get("value", "")),
        placeholder=str(raw.get("placeholder", "")),
        editable=bool(raw.get("editable")),
        disabled=bool(raw.get("disabled")),
        frame_id=str(raw.get("frame_id", "")),
        attributes=dict(raw.get("attributes") or {}),
    )


def dump_snapshot(snapshot: Snapshot) -> dict[str, Any]:
    return {
        "url": snapshot.url,
        "title": snapshot.title,
        "viewport": dump_viewport(snapshot.viewport),
        "elements": [dump_element(e) for e in snapshot.elements],
        "text": snapshot.text,
        "signature": snapshot.signature,
        "captured_at": snapshot.captured_at,
        "frames": [
            {
                "frame_id": f.frame_id,
                "url": f.url,
                "parent_id": f.parent_id,
                "is_main": f.is_main,
                "cross_origin": f.cross_origin,
            }
            for f in snapshot.frames
        ],
        "extra": dict(snapshot.extra),
    }


def load_snapshot(raw: Mapping[str, Any]) -> Snapshot:
    return Snapshot(
        url=str(raw.get("url", "")),
        title=str(raw.get("title", "")),
        viewport=load_viewport(raw.get("viewport") or {}),
        elements=tuple(load_element(e) for e in raw.get("elements") or ()),
        text=str(raw.get("text", "")),
        signature=str(raw.get("signature", "")),
        captured_at=float(raw.get("captured_at", 0.0)),
        frames=tuple(
            FrameInfo(
                frame_id=str(f.get("frame_id", "")),
                url=str(f.get("url", "")),
                parent_id=str(f.get("parent_id", "")),
                is_main=bool(f.get("is_main")),
                cross_origin=bool(f.get("cross_origin")),
            )
            for f in raw.get("frames") or ()
        ),
        extra=dict(raw.get("extra") or {}),
    )


def dump_screenshot(shot: Screenshot) -> dict[str, Any]:
    return {
        # base64 under its own key: JSON has no byte type, and a binary payload
        # pushed through a string field corrupts on the first non-UTF-8 byte.
        "data_b64": base64.b64encode(shot.data).decode("ascii"),
        "format": shot.format,
        "width": shot.width,
        "height": shot.height,
        "device_pixel_ratio": shot.device_pixel_ratio,
        "full_page": shot.full_page,
    }


def load_screenshot(raw: Mapping[str, Any]) -> Screenshot:
    return Screenshot(
        data=base64.b64decode(raw.get("data_b64") or ""),
        format=str(raw.get("format", "png")),
        width=int(raw.get("width", 0)),
        height=int(raw.get("height", 0)),
        device_pixel_ratio=float(raw.get("device_pixel_ratio", 1.0)),
        full_page=bool(raw.get("full_page")),
    )


def dump_outcome(outcome: ActionOutcome) -> dict[str, Any]:
    return {
        "ok": outcome.ok,
        "changed": outcome.changed,
        "detail": outcome.detail,
        "data": dict(outcome.data),
    }


def load_outcome(raw: Mapping[str, Any]) -> ActionOutcome:
    return ActionOutcome(
        ok=bool(raw.get("ok")),
        changed=bool(raw.get("changed")),
        detail=str(raw.get("detail", "")),
        data=dict(raw.get("data") or {}),
    )


def dump_navigation(result: NavigationResult) -> dict[str, Any]:
    return {
        "url": result.url,
        "ok": result.ok,
        "status": result.status,
        "redirected": result.redirected,
        "error": result.error,
    }


def load_navigation(raw: Mapping[str, Any]) -> NavigationResult:
    return NavigationResult(
        url=str(raw.get("url", "")),
        ok=bool(raw.get("ok", True)),
        status=int(raw.get("status", 0)),
        redirected=bool(raw.get("redirected")),
        error=str(raw.get("error", "")),
    )


def dump_tab(tab: TabInfo) -> dict[str, Any]:
    return {
        "tab_id": tab.tab_id,
        "url": tab.url,
        "title": tab.title,
        "active": tab.active,
        "window_id": tab.window_id,
        "attached": tab.attached,
    }


def load_tab(raw: Mapping[str, Any]) -> TabInfo:
    return TabInfo(
        tab_id=str(raw.get("tab_id", "")),
        url=str(raw.get("url", "")),
        title=str(raw.get("title", "")),
        active=bool(raw.get("active")),
        window_id=str(raw.get("window_id", "")),
        attached=bool(raw.get("attached")),
    )


def load_point_or_element(raw: Mapping[str, Any]) -> Point | Element:
    """A click target arrives as one or the other; the payload says which."""
    if "handle" in raw:
        return load_element(raw)
    return Point(float(raw.get("x", 0.0)), float(raw.get("y", 0.0)))
