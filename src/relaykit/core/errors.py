"""The exception hierarchy every engine, transport and model raises.

Backends must translate their native failures into these. A caller that catches
:class:`EngineError` should not then have to catch ``websockets.ConnectionClosed``
or ``playwright.TimeoutError`` as well -- if it does, the backend is leaking and
that is a bug in the backend.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "ActionFailed",
    "CapabilityNotSupported",
    "ElementNotFound",
    "EngineError",
    "EngineNotAvailable",
    "EvaluationError",
    "FrameNotFound",
    "ModelError",
    "NavigationError",
    "PermissionDenied",
    "ProtocolError",
    "RelayKitError",
    "StaleHandle",
    "TabNotFound",
    "TimeoutError",
    "TransportError",
]


class RelayKitError(Exception):
    """Base class for everything this project raises."""

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context: Mapping[str, Any] = context

    def __str__(self) -> str:
        if not self.context:
            return self.message
        rendered = ", ".join(f"{k}={v!r}" for k, v in self.context.items())
        return f"{self.message} ({rendered})"


# --------------------------------------------------------------------------- #
# Engine                                                                       #
# --------------------------------------------------------------------------- #


class EngineError(RelayKitError):
    """A browser backend failed."""


class EngineNotAvailable(EngineError):
    """The backend cannot run here at all.

    Raise this from ``Engine.probe()`` -- Safari on Linux, a Chrome engine with
    no Chrome installed, an extension backend with no extension loaded. It is
    distinct from a runtime failure: the registry uses it to fall through to the
    next candidate engine rather than to abort.
    """


class CapabilityNotSupported(EngineError):
    """The backend is running but structurally cannot do this.

    Never raise this for a transient failure. It means "no implementation of me
    will ever do this", e.g. asking a WebDriver backend to attach to the user's
    existing window. Callers may branch on it to pick another route.
    """


class TabNotFound(EngineError):
    pass


class FrameNotFound(EngineError):
    pass


class ElementNotFound(EngineError):
    pass


class StaleHandle(ElementNotFound):
    """The handle was valid, the page moved underneath it.

    Distinguished from :class:`ElementNotFound` because the correct response
    differs: re-snapshot and retry, rather than conclude the element is absent.
    """


class NavigationError(EngineError):
    pass


class ActionFailed(EngineError):
    pass


class EvaluationError(EngineError):
    """Script evaluation raised inside the page."""


class TimeoutError(EngineError):
    pass


# --------------------------------------------------------------------------- #
# Transport / daemon                                                           #
# --------------------------------------------------------------------------- #


class TransportError(RelayKitError):
    """The daemon connection failed."""


class ProtocolError(TransportError):
    """A message did not match the wire schema."""


class PermissionDenied(RelayKitError):
    """Refused by policy -- auth token, sandbox rule, or a confirmation gate."""


# --------------------------------------------------------------------------- #
# Models                                                                       #
# --------------------------------------------------------------------------- #


class ModelError(RelayKitError):
    """An LLM provider failed."""
