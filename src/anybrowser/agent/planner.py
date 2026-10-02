"""``Planner`` -- decides the next action.

Swapping the planner is how you change what kind of agent this is. The default
one is an LLM reading a screenshot and a snapshot; a scripted planner replaying a
recorded workflow, or a heuristic one, implements the same two methods and runs
on the same executor, tools and engines.
"""

from __future__ import annotations

import abc
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..core.types import Screenshot, Snapshot
from .tool import Tool, ToolResult

__all__ = ["Decision", "Observation", "Planner"]


@dataclass(frozen=True, slots=True)
class Observation:
    """Everything the planner is allowed to see this step."""

    goal: str
    snapshot: Snapshot
    screenshot: Screenshot | None = None
    history: Sequence[Step] = ()
    notes: Sequence[str] = ()
    step_index: int = 0
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Decision:
    """One chosen action.

    ``narrative`` is required and must be a human explanation of *why*, not a
    restatement of the tool call. Models with no visible reasoning still have to
    produce one; a run whose history reads "clicked, clicked, clicked" cannot be
    debugged by anyone, including the model itself on the next step.
    """

    tool: str
    arguments: Mapping[str, Any]
    narrative: str
    done: bool = False
    answer: str = ""

    def __post_init__(self) -> None:
        if not self.narrative.strip():
            raise ValueError("Decision.narrative is required")


@dataclass(frozen=True, slots=True)
class Step:
    """One completed (decision, result) pair, as history renders it."""

    decision: Decision
    result: ToolResult


class Planner(abc.ABC):
    """Chooses the next tool call."""

    #: Registry name, if published as a plugin.
    name: str = ""

    @abc.abstractmethod
    async def decide(self, observation: Observation, tools: Sequence[Tool]) -> Decision: ...

    async def reflect(self, observation: Observation, step: Step) -> None:
        """Optional hook after each step, for memory or a stuck detector."""
        return None
