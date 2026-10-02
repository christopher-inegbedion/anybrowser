"""Interfaces and value types. Imports no backend."""

from .engine import BrowserEngine, Capabilities, Capability, EngineInfo
from .errors import (
    ActionFailed,
    AnyBrowserError,
    CapabilityNotSupported,
    ElementNotFound,
    EngineError,
    EngineNotAvailable,
    NavigationError,
    StaleHandle,
    TransportError,
)
from .registry import engines, models, transports
from .sync import SyncEngine
from .types import (
    ActionOutcome,
    Box,
    Element,
    FrameInfo,
    KeyModifier,
    MouseButton,
    NavigationResult,
    Point,
    Screenshot,
    Snapshot,
    TabInfo,
    Viewport,
)

__all__ = [
    "ActionFailed",
    "ActionOutcome",
    "AnyBrowserError",
    "Box",
    "BrowserEngine",
    "Capabilities",
    "Capability",
    "CapabilityNotSupported",
    "Element",
    "ElementNotFound",
    "EngineError",
    "EngineInfo",
    "EngineNotAvailable",
    "FrameInfo",
    "KeyModifier",
    "MouseButton",
    "NavigationError",
    "NavigationResult",
    "Point",
    "Screenshot",
    "Snapshot",
    "StaleHandle",
    "SyncEngine",
    "TabInfo",
    "TransportError",
    "Viewport",
    "engines",
    "models",
    "transports",
]
