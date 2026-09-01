"""Talking to the Swift helper that owns Safari's native layer.

One resident subprocess, one JSON object per line, request/response correlated
by id. Keeping it resident is the whole design: the helper holds a warm
accessibility connection and a live ``NSAppleScript`` instance, which costs
about 5ms per call against roughly 80ms for spawning ``osascript``. On a run
doing hundreds of actions that difference is the engine feeling responsive or
not.

The bridge is deliberately thin. It knows about processes and framing; what the
commands *mean* is :mod:`relaykit.engines.safari.engine`'s problem.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...core.errors import EngineError, EngineNotAvailable

logger = logging.getLogger(__name__)

__all__ = ["EngineStatus", "SafariBridge", "SafariBridgeError"]


class SafariBridgeError(EngineError):
    """The helper failed or refused."""


@dataclass(frozen=True, slots=True)
class EngineStatus:
    running: bool
    ax_trusted: bool
    safari_running: bool
    detail: str = ""


class SafariBridge:
    """A resident Swift helper, spoken to over stdio."""

    def __init__(self, app_path: Path) -> None:
        self._app_path = Path(app_path)
        self._binary = self._app_path / "Contents" / "MacOS" / "RelayKitSafariEngine"
        self._process: asyncio.subprocess.Process | None = None
        self._seq = 0
        # One command at a time. The wire protocol correlates by id and would
        # survive interleaving, but the helper's AX work is not reentrant.
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        if self._process is not None and self._process.returncode is None:
            return
        if not self._binary.exists():
            raise EngineNotAvailable(
                f"the Safari helper is not built at {self._binary}; "
                "call relaykit.engines.safari.build.build_engine()",
            )
        self._process = await asyncio.create_subprocess_exec(
            str(self._binary),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ},
        )

    async def close(self) -> None:
        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(Exception):
            if process.stdin is not None:
                # The helper exits on stdin close, which is cleaner than a
                # signal: it gets to release its AX connection first.
                process.stdin.close()
        with contextlib.suppress(Exception, asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=3)
        if process.returncode is None:
            with contextlib.suppress(Exception):
                process.kill()

    # ------------------------------------------------------------------ #
    # Calls                                                               #
    # ------------------------------------------------------------------ #

    async def call(self, cmd: str, *, timeout: float = 30.0, **params: Any) -> dict[str, Any]:
        async with self._lock:
            await self.start()
            process = self._process
            if process is None or process.stdin is None or process.stdout is None:
                raise SafariBridgeError("the Safari helper is not running")

            self._seq += 1
            message_id = str(self._seq)
            payload = json.dumps({"cmd": cmd, "id": message_id, **params})
            process.stdin.write(payload.encode("utf-8") + b"\n")
            await process.stdin.drain()

            async def _await_reply() -> dict[str, Any]:
                assert process.stdout is not None
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        self._process = None
                        raise SafariBridgeError(f"the helper exited during {cmd!r}")
                    try:
                        reply = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # Events (no id) share the stream with replies; skip anything
                    # that is not the answer we are waiting for.
                    if reply.get("id") != message_id:
                        continue
                    return reply

            try:
                reply = await asyncio.wait_for(_await_reply(), timeout)
            except asyncio.TimeoutError as exc:
                raise SafariBridgeError(f"{cmd} timed out after {timeout}s") from exc

        if not reply.get("ok"):
            raise SafariBridgeError(str(reply.get("error") or f"{cmd} failed"))
        return reply

    # ------------------------------------------------------------------ #
    # Commands                                                            #
    # ------------------------------------------------------------------ #

    async def status(self) -> EngineStatus:
        try:
            reply = await self.call("ping", timeout=5)
        except (SafariBridgeError, EngineNotAvailable) as exc:
            return EngineStatus(False, False, False, str(exc))
        return EngineStatus(
            running=True,
            ax_trusted=bool(reply.get("axTrusted")),
            safari_running=bool(reply.get("safari")),
        )

    async def windows(self) -> list[dict[str, Any]]:
        """Safari's windows with their web-area geometry, in screen points."""
        return list((await self.call("windows")).get("windows") or [])

    async def hit(self, *, window: str, x: float, y: float, url: str = "") -> dict[str, Any]:
        """What is at this page coordinate: role, title, and available actions.

        Always pass ``url`` when it is known. Window titles are page titles, so
        several windows routinely share one and matching by title alone picks
        whichever comes first -- acting on a page that merely looks like the
        target. The web area's AXURL identifies it exactly.
        """
        reply = await self.call("hit", window=window, x=x, y=y, url=url)
        return dict(reply.get("element") or {})

    async def press(self, *, window: str, x: float, y: float, url: str = "") -> dict[str, Any]:
        """A trusted click, in the background, without moving the cursor.

        Resolves the *actionable* element, not the deepest one: the deepest node
        under a point is usually an inner span that cannot be pressed, so the
        helper walks up to the nearest ancestor that can be and fails loudly
        when there is none, rather than reporting a press that did nothing.
        """
        reply = await self.call("press", window=window, x=x, y=y, url=url)
        return dict(reply.get("element") or {})

    async def fill(
        self,
        *,
        window: str,
        x: float,
        y: float,
        text: str,
        url: str = "",
        allow_raise: bool = True,
    ) -> dict[str, Any]:
        """Set a field's value, verifying that it took.

        Setting ``AXValue`` is honoured without focus on some controls and
        silently dropped on others, returning success either way, so the helper
        writes, reads back, and escalates to focusing only when the write did
        not land. Focusing raises Safari, so the reply says whether that
        happened; ``allow_raise=False`` forbids it and produces a clean failure
        instead of a surprise window activation.
        """
        return await self.call(
            "fill", window=window, x=x, y=y, text=text, url=url, allowRaise=allow_raise
        )

    async def dialog(self, action: str = "peek") -> dict[str, Any]:
        """Inspect or answer a native JavaScript dialog: peek | accept | dismiss."""
        return await self.call("dialog", action=action)

    async def screenshot(self, *, window: str = "", crop: bool = True) -> bytes:
        """PNG of a Safari window, even when it is occluded or backgrounded."""
        reply = await self.call("screenshot", window=window, crop=crop)
        return base64.b64decode(reply["png"])

    async def applescript(self, source: str) -> str:
        """Run AppleScript on the resident interpreter: tabs, windows, navigation."""
        return str((await self.call("applescript", source=source)).get("result") or "")
