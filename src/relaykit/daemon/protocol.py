"""The daemon wire protocol.

One long-lived, bidirectional, JSON-framed session. Requests carry an ``id`` and
get exactly one response; events carry no ``id`` and are never acknowledged.
That is the whole thing -- deliberately close to JSON-RPC 2.0 so an
implementation in another language is an afternoon, not a project.

    -> {"id": "7", "method": "engine.click", "params": {"handle": "e12"}}
    <- {"id": "7", "ok": true, "result": {"changed": true}}
    <- {"event": "tab.navigated", "data": {"tab_id": "3", "url": "..."}}

Versioning
----------
:data:`PROTOCOL_VERSION` is bumped on any breaking change to a method's params or
result. The server sends it in the ``hello`` event on connect; a client that
cannot speak it must disconnect rather than guess. Additive changes -- new
methods, new optional params, new event types -- do not bump it, and clients
must ignore unknown events and unknown result fields.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..core.errors import ProtocolError

__all__ = [
    "PROTOCOL_VERSION",
    "Event",
    "Message",
    "Request",
    "Response",
    "decode",
    "encode",
]

#: Bumped only for breaking changes. See the module docstring.
PROTOCOL_VERSION = 1


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    params: Mapping[str, Any] = field(default_factory=dict)
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            object.__setattr__(self, "id", uuid.uuid4().hex[:12])

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "method": self.method, "params": dict(self.params)}


@dataclass(frozen=True, slots=True)
class Response:
    id: str
    ok: bool = True
    result: Any = None
    error: str = ""
    error_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "ok": self.ok}
        if self.ok:
            out["result"] = self.result
        else:
            out["error"] = self.error
            out["error_type"] = self.error_type
        return out


@dataclass(frozen=True, slots=True)
class Event:
    event: str
    data: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"event": self.event, "data": dict(self.data)}


Message = Request | Response | Event


def encode(message: Message) -> str:
    """Serialise one message to a single line of JSON (no embedded newlines)."""
    return json.dumps(message.to_dict(), separators=(",", ":"), ensure_ascii=False)


def decode(raw: str | bytes) -> Message:
    """Parse one line into the right message type.

    Raises :class:`~relaykit.core.errors.ProtocolError` on anything malformed --
    a transport must never hand a half-understood message to the server.
    """
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("message was not valid UTF-8") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"message was not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError(f"message must be an object, got {type(payload).__name__}")

    if "event" in payload:
        return Event(event=str(payload["event"]), data=payload.get("data") or {})
    if "method" in payload:
        return Request(
            method=str(payload["method"]),
            params=payload.get("params") or {},
            id=str(payload.get("id") or ""),
        )
    if "id" in payload and ("ok" in payload or "result" in payload or "error" in payload):
        return Response(
            id=str(payload["id"]),
            ok=bool(payload.get("ok", "error" not in payload)),
            result=payload.get("result"),
            error=str(payload.get("error") or ""),
            error_type=str(payload.get("error_type") or ""),
        )
    raise ProtocolError("message is neither a request, a response, nor an event")
