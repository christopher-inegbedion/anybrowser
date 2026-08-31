"""Interfaces and value types. Imports no backend."""

from .engine import BrowserEngine, Capabilities, Capability, EngineInfo
from .errors import (
    ActionFailed,
    CapabilityNotSupported,
    ElementNotFound,
    EngineError,
    EngineNotAvailable,
    NavigationError,
    RelayKitError,
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
    "RelayKitError",
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
