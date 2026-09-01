"""Build the Swift helper that drives Safari's native layer.

Compiling at install time rather than shipping a binary keeps the wheel
platform-neutral and, more importantly, lets each embedder stamp **their own
bundle identifier** into the helper. That is not a nicety -- see
:func:`build_engine` for why it decides whether the engine works at all.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from ...core.errors import EngineNotAvailable

__all__ = ["DEFAULT_BUNDLE_ID", "build_engine", "engine_app_path", "swift_available"]

_HERE = Path(__file__).parent
_SOURCE = _HERE / "mac_engine" / "RelayKitSafariEngine.swift"
_PLIST = _HERE / "mac_engine" / "Info.plist.in"

APP_NAME = "RelayKit Safari Engine.app"
_EXECUTABLE = Path("Contents") / "MacOS" / "RelayKitSafariEngine"

#: Placeholder identity. An engine built with this has no Accessibility grant
#: and every input call will fail -- deliberately, and loudly, rather than
#: appearing to work.
DEFAULT_BUNDLE_ID = "dev.relaykit.safari-engine"


def on_macos() -> bool:
    """Whether this is macOS.

    Wrapped in a function rather than compared inline because a type checker
    narrows a literal ``sys.platform`` comparison to a constant for whichever
    platform it is running on, and then reports every macOS-only branch as
    unreachable on Linux -- or the reverse. The behaviour is identical; only the
    checker's view of it changes.
    """
    return sys.platform == "darwin"


def swift_available() -> bool:
    return on_macos() and shutil.which("swiftc") is not None


def engine_app_path(search: list[Path] | None = None) -> Path | None:
    """Locate a built helper, most authoritative first."""
    override = os.environ.get("RELAYKIT_SAFARI_ENGINE_APP", "").strip()
    if override:
        candidate = Path(override).expanduser()
        return candidate if (candidate / _EXECUTABLE).exists() else None
    candidates = list(search or [])
    candidates.append(Path.home() / ".relaykit" / APP_NAME)
    for candidate in candidates:
        if (candidate / _EXECUTABLE).exists():
            return candidate
    return None


def build_engine(
    out_dir: Path | str,
    *,
    bundle_id: str = DEFAULT_BUNDLE_ID,
    app_name: str = "RelayKit",
    version: str = "0.1.0",
) -> Path:
    """Compile the helper into ``out_dir`` and return the .app path.

    ``bundle_id`` must be **the identifier of the application that ships this
    helper**, not a new one. macOS keys its permission database on code
    identity: the Accessibility grant the user gave your app covers this helper
    only if the two identifiers match. A fresh identifier produces a second
    entry in Privacy & Security that the user must find and approve separately,
    and is frequently refused with no prompt at all -- which presents as an
    engine that silently cannot click anything.
    """
    if not on_macos():
        raise EngineNotAvailable("the Safari engine only builds on macOS")
    if not swift_available():
        raise EngineNotAvailable(
            "swiftc not found; install the Xcode command line tools (xcode-select --install)"
        )

    out_dir = Path(out_dir).expanduser()
    app = out_dir / APP_NAME
    macos_dir = app / "Contents" / "MacOS"
    macos_dir.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        [
            "swiftc",
            "-O",
            "-framework",
            "AppKit",
            "-framework",
            "ApplicationServices",
            "-framework",
            "SafariServices",
            "-framework",
            "ScreenCaptureKit",
            "-framework",
            "ImageIO",
            "-o",
            str(macos_dir / "RelayKitSafariEngine"),
            str(_SOURCE),
        ],
        check=True,
        capture_output=True,
    )

    template = _PLIST.read_text()
    for token, value in (
        ("__BUNDLE_ID__", bundle_id),
        ("__APP_NAME__", app_name),
        ("__VERSION__", version),
    ):
        template = template.replace(token, value)
    plist_path = app / "Contents" / "Info.plist"
    plist_path.write_text(template)
    # Parse it back: a malformed plist produces an app bundle macOS refuses to
    # launch with an error that names neither the file nor the problem.
    plistlib.loads(plist_path.read_bytes())
    return app
