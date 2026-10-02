"""Safari engine: accessibility for input, a Web Extension for perception.

Both halves are implemented: the Swift accessibility helper for trusted
background input and occlusion-proof capture, and the Web Extension for the DOM
and pointer gestures. Each needs a local build and a one-time Accessibility
grant, so ``probe()`` refuses cleanly when the helper is unbuilt or the platform
is not macOS -- the registry then falls through to another engine rather than
failing mid-run. The native half is usable on its own via
:class:`~relaykit.engines.safari.bridge.SafariBridge`.
"""

from .bridge import EngineStatus, SafariBridge, SafariBridgeError
from .build import (
    DEFAULT_BUNDLE_ID,
    build_engine,
    engine_app_path,
    on_macos,
    swift_available,
)
from .engine import SUPPORTED_CAPABILITIES, SafariEngine

__all__ = [
    "DEFAULT_BUNDLE_ID",
    "SUPPORTED_CAPABILITIES",
    "EngineStatus",
    "SafariBridge",
    "SafariBridgeError",
    "SafariEngine",
    "build_engine",
    "engine_app_path",
    "on_macos",
    "swift_available",
]
