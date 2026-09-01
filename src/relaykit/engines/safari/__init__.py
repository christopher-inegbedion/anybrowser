"""Safari engine: accessibility for input, a Web Extension for perception.

Mid-port. ``probe()`` refuses cleanly until the extension half exists, so the
registry falls through to another engine rather than failing mid-run. The native
half -- the Swift accessibility helper -- is ported and usable on its own via
:class:`~relaykit.engines.safari.bridge.SafariBridge`.
"""

from .bridge import EngineStatus, SafariBridge, SafariBridgeError
from .build import DEFAULT_BUNDLE_ID, build_engine, engine_app_path, swift_available
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
    "swift_available",
]
