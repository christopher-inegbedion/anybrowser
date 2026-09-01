"""Chrome engine capability declaration and public export."""

from __future__ import annotations

from ...core.engine import Capabilities, Capability

__all__ = ["PLANNED_CAPABILITIES", "ChromeEngine"]

#: What the finished engine will declare. Written down first so the conformance
#: run is a real gate rather than a description of whatever got built.
PLANNED_CAPABILITIES = Capabilities.of(
    Capability.ATTACH_TO_USER_SESSION,
    Capability.TRUSTED_INPUT,
    Capability.BACKGROUND_INPUT,
    Capability.EVALUATE_JS,
    Capability.CROSS_ORIGIN_FRAMES,
    Capability.OFFSCREEN_SCREENSHOT,
    Capability.FULL_PAGE_SCREENSHOT,
    Capability.SCREENCAST,
    Capability.POINTER_GESTURES,
    Capability.FILE_UPLOAD,
    Capability.JS_DIALOGS,
    Capability.COOKIES,
    Capability.NETWORK_INTERCEPTION,
    Capability.TAB_MANAGEMENT,
    Capability.PAGE_ZOOM,
    Capability.INIT_SCRIPTS,
)


from .engine import ChromeEngine  # noqa: E402
