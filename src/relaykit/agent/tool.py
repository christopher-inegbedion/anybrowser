"""``Tool`` -- one thing an agent can do.

A tool is a name, a schema, and a callable. It is deliberately not tied to any
model's function-calling format: the planner renders tools into whatever the
model wants, so a tool written once works against every provider.

    class Click(Tool):
        name = "click"
        description = "Click an element on the page."
        parameters = {
            "type": "object",
            "properties": {"handle": {"type": "string"}},
            "required": ["handle"],
        }

        async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
            outcome = await ctx.engine.click(ctx.element(kwargs["handle"]))
            return ToolResult.of(outcome)
"""

from __future__ import annotations

import abc
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, ClassVar

from ..core.engine import BrowserEngine
from ..core.types import ActionOutcome, Element, Snapshot

__all__ = ["Tool", "ToolContext", "ToolResult"]


@dataclass(slots=True)
class ToolContext:
    """What a tool is given: the browser, the last observation, and scratch space."""

    engine: BrowserEngine
    snapshot: Snapshot | None = None
    state: dict[str, Any] = field(default_factory=dict)

    def element(self, handle: str) -> Element:
        """Resolve a handle against the current snapshot, or raise."""
        from ..core.errors import ElementNotFound, StaleHandle

        if self.snapshot is None:
            raise ElementNotFound("no snapshot has been taken", handle=handle)
        found = self.snapshot.element(handle)
        if found is None:
            raise StaleHandle("handle is not in the current snapshot", handle=handle)
        return found


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What a tool did, in the form the agent's history will render.

    ``changed`` carries through from :class:`~relaykit.core.types.ActionOutcome`
    for one reason: history that renders a no-op as "success" teaches the model
    that repeating it is progress, and it will repeat it until the run dies.
    """

    ok: bool
    changed: bool = True
    summary: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def of(cls, outcome: ActionOutcome, summary: str = "") -> ToolResult:
        return cls(
            ok=outcome.ok,
            changed=outcome.changed,
            summary=summary or outcome.detail,
            data=dict(outcome.data),
        )

    @classmethod
    def failure(cls, summary: str, **data: Any) -> ToolResult:
        return cls(ok=False, changed=False, summary=summary, data=data)


class Tool(abc.ABC):
    """One capability exposed to the planner."""

    #: Stable identifier the model emits. Renaming one is a breaking change.
    name: str = ""
    #: One sentence, written for the model rather than for a human reader.
    description: str = ""
    #: JSON Schema for the arguments.
    parameters: ClassVar[Mapping[str, Any]] = MappingProxyType({})
    #: Tools that change the world are gated by the confirmation policy before
    #: they run. Read-only tools are not. Default to True when unsure.
    mutating: bool = True

    @abc.abstractmethod
    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult: ...

    def schema(self) -> dict[str, Any]:
        """Provider-neutral description; the planner adapts it per model."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": dict(self.parameters) or {"type": "object", "properties": {}},
        }
