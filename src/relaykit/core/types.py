"""Value types shared by every layer.

These are deliberately plain dataclasses with no engine, transport or model
imports. An engine author should be able to implement a backend knowing only
this module and :mod:`relaykit.core.engine`.

Coordinates
-----------
Two coordinate spaces exist and they are never mixed:

``viewport``
    CSS pixels from the top-left of the visible viewport, unscaled by device
    pixel ratio. Every :class:`Point` handed to an engine is in this space.

``normalized``
    0-1000 on both axes within some *surface* (the viewport, or a canvas, or an
    element box). Models emit this space because it is resolution independent.
    Convert with :meth:`Point.from_normalized` at the boundary -- engines never
    see normalized values.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "ActionOutcome",
    "Box",
    "Element",
    "FrameInfo",
    "KeyModifier",
    "MouseButton",
    "NavigationResult",
    "Point",
    "Screenshot",
    "Snapshot",
    "TabInfo",
    "Viewport",
]


@dataclass(frozen=True, slots=True)
class Point:
    """A location in viewport CSS pixels."""

    x: float
    y: float

    @classmethod
    def from_normalized(cls, nx: float, ny: float, surface: Box) -> Point:
        """Map a 0-1000 point inside ``surface`` onto viewport pixels."""
        if not 0 <= nx <= 1000 or not 0 <= ny <= 1000:
            raise ValueError(f"normalized point out of range: ({nx}, {ny})")
        return cls(
            x=surface.x + (nx / 1000.0) * surface.width,
            y=surface.y + (ny / 1000.0) * surface.height,
        )

    def as_tuple(self) -> tuple[float, float]:
        return (self.x, self.y)


@dataclass(frozen=True, slots=True)
class Box:
    """An axis-aligned rectangle in viewport CSS pixels."""

    x: float
    y: float
    width: float
    height: float

    @property
    def center(self) -> Point:
        return Point(self.x + self.width / 2, self.y + self.height / 2)

    @property
    def area(self) -> float:
        return self.width * self.height

    def contains(self, point: Point) -> bool:
        return (
            self.x <= point.x <= self.x + self.width and self.y <= point.y <= self.y + self.height
        )

    def intersects(self, other: Box) -> bool:
        return not (
            other.x > self.x + self.width
            or other.x + other.width < self.x
            or other.y > self.y + self.height
            or other.y + other.height < self.y
        )


@dataclass(frozen=True, slots=True)
class Viewport:
    """Visible page area, plus how it sits inside the full document."""

    width: int
    height: int
    scroll_x: float = 0.0
    scroll_y: float = 0.0
    device_pixel_ratio: float = 1.0
    page_zoom: float = 1.0

    @property
    def box(self) -> Box:
        return Box(0, 0, float(self.width), float(self.height))


class MouseButton(str, Enum):
    LEFT = "left"
    MIDDLE = "middle"
    RIGHT = "right"


class KeyModifier(str, Enum):
    ALT = "alt"
    CONTROL = "control"
    META = "meta"
    SHIFT = "shift"


@dataclass(frozen=True, slots=True)
class Element:
    """One interactive element as perception found it.

    ``handle`` is opaque and engine-owned: it is whatever that engine needs to
    address the element again (a CDP backend node id, an extension-assigned
    integer, an accessibility element ref). Callers pass it back verbatim and
    must not parse it. It is valid only until the page navigates.
    """

    handle: str
    box: Box
    tag: str = ""
    role: str = ""
    label: str = ""
    value: str = ""
    placeholder: str = ""
    editable: bool = False
    disabled: bool = False
    frame_id: str = ""
    attributes: Mapping[str, str] = field(default_factory=dict)

    @property
    def description(self) -> str:
        """Short human-readable identity, for logs and model prompts."""
        parts = [p for p in (self.role or self.tag, self.label or self.placeholder) if p]
        return " ".join(parts) or self.handle


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One observation of a page: what is on it and where.

    A snapshot is a *value*. It is never live -- reading ``elements`` after the
    page moved gives you the old page, which is the point: planners reason about
    a fixed frame and the executor re-resolves against a fresh one.
    """

    url: str
    title: str
    viewport: Viewport
    elements: Sequence[Element] = ()
    text: str = ""
    signature: str = ""
    captured_at: float = 0.0
    frames: Sequence[FrameInfo] = ()
    extra: Mapping[str, Any] = field(default_factory=dict)

    def element(self, handle: str) -> Element | None:
        for el in self.elements:
            if el.handle == handle:
                return el
        return None


@dataclass(frozen=True, slots=True)
class Screenshot:
    """Encoded page pixels."""

    data: bytes
    format: str = "png"
    width: int = 0
    height: int = 0
    device_pixel_ratio: float = 1.0
    full_page: bool = False


@dataclass(frozen=True, slots=True)
class TabInfo:
    tab_id: str
    url: str = ""
    title: str = ""
    active: bool = False
    window_id: str = ""
    attached: bool = False


@dataclass(frozen=True, slots=True)
class FrameInfo:
    frame_id: str
    url: str = ""
    parent_id: str = ""
    is_main: bool = False
    cross_origin: bool = False


@dataclass(frozen=True, slots=True)
class NavigationResult:
    url: str
    ok: bool = True
    status: int = 0
    redirected: bool = False
    error: str = ""


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    """What an action did -- reported truthfully, including doing nothing.

    ``changed`` is the field that matters. An engine that clicked a dead pixel
    must report ``ok=True, changed=False``: agents loop forever when a no-op is
    rendered to them as success. See ``docs/architecture/truthful-outcomes.md``.
    """

    ok: bool
    changed: bool = True
    detail: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def no_change(cls, detail: str, **data: Any) -> ActionOutcome:
        return cls(ok=True, changed=False, detail=detail, data=data)

    @classmethod
    def failure(cls, detail: str, **data: Any) -> ActionOutcome:
        return cls(ok=False, changed=False, detail=detail, data=data)
