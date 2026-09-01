"""Chrome engine: CDP over one of two pipes.

``devtools``
    A direct WebSocket to a browser started with ``--remote-debugging-port``.
    Standard and CI-able, but the flag must be present from launch, so it can
    only ever reach a browser started for automation.

``extension``
    CDP relayed through the RelayKit browser extension, which holds a
    ``chrome.debugger`` session. This one attaches to the browser the user
    already has open -- their windows, their tabs, their logins -- which is the
    reason this engine exists. Load ``extensions/chrome`` and point it at the
    engine's port.

Both speak the same CDP; only the pipe differs, so the engine is written once
(:mod:`relaykit.engines.chrome.cdp`). The connection reports whether it reaches
a real user session, and the declared ``ATTACH_TO_USER_SESSION`` capability is
derived from that rather than asserted alongside it.
"""

from .cdp import CdpConnection, DevToolsConnection, ExtensionConnection
from .engine import PLANNED_CAPABILITIES, ChromeEngine

__all__ = [
    "PLANNED_CAPABILITIES",
    "CdpConnection",
    "ChromeEngine",
    "DevToolsConnection",
    "ExtensionConnection",
]
