"""The loop: observe, decide, act, remember.

    runner = AgentRunner(engine, planner)
    result = await runner.run("find the pricing page and read the top tier")

Deliberately small. Everything that decides *quality* lives in the planner and
in perception; this file's whole job is to be an honest, interruptible loop that
never lies to the planner about what happened.

Three properties it guarantees, each of which is a failure mode somewhere else:

**It re-observes before every decision.** A planner reasoning about a snapshot
taken three actions ago is reasoning about a page that no longer exists, and
its handles are stale by definition.

**It records no-ops as no-ops.** ``ToolResult.changed`` goes into history
verbatim. A history that renders "clicked, success" for a click that hit an
invisible overlay teaches the model that clicking again is progress, and it
will do that until the budget runs out.

**It stops.** A budget, a stop signal, and a repetition breaker. Long-horizon
agents do not fail by crashing; they fail by continuing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from ..core.engine import BrowserEngine
from ..core.errors import RelayKitError
from ..core.types import Screenshot
from .planner import Decision, Observation, Planner, Step
from .tool import Tool, ToolContext, ToolResult
from .tools import default_tools

logger = logging.getLogger(__name__)

__all__ = ["AgentRunner", "RunConfig", "RunResult", "StopReason"]


class StopReason(str):
    """Why a run ended. A plain string subclass so it renders itself in logs."""

    DONE = "done"
    BUDGET = "budget_exhausted"
    STOPPED = "stopped"
    STUCK = "stuck"
    ERROR = "error"


@dataclass(slots=True)
class RunConfig:
    """Limits. All of them exist because something ran away without them."""

    #: Hard ceiling on actions. The single most important safety valve.
    max_steps: int = 40
    #: Wall-clock ceiling, in seconds. Zero disables it.
    max_seconds: float = 600.0
    #: Give the planner a screenshot alongside the snapshot.
    include_screenshot: bool = True
    #: How many consecutive actions may change nothing before the run is called
    #: stuck. Counts any action, not just repeats -- see AgentRunner.run for why
    #: repetition is the wrong thing to measure. Four leaves room for a genuine
    #: retry and a look in between.
    repeat_limit: int = 4
    #: Confirm before a mutating tool runs. Returning False refuses the action;
    #: the planner is told, and can choose something else.
    confirm: Callable[[Decision], bool] | None = None


@dataclass(slots=True)
class RunResult:
    goal: str
    answer: str = ""
    stop_reason: str = StopReason.DONE
    steps: list[Step] = field(default_factory=list)
    error: str = ""
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return self.stop_reason == StopReason.DONE

    @property
    def step_count(self) -> int:
        return len(self.steps)


class AgentRunner:
    """Drives a planner against a browser until it is done or out of budget."""

    def __init__(
        self,
        engine: BrowserEngine,
        planner: Planner,
        *,
        tools: Sequence[Tool] | None = None,
        config: RunConfig | None = None,
    ) -> None:
        self._engine = engine
        self._planner = planner
        self._tools = list(tools) if tools is not None else default_tools()
        self._config = config or RunConfig()
        self._by_name = {tool.name: tool for tool in self._tools}
        self._stop = asyncio.Event()

    @property
    def tools(self) -> Sequence[Tool]:
        return tuple(self._tools)

    def stop(self) -> None:
        """Ask the run to finish after the current action.

        Cooperative rather than a cancellation, because killing an agent
        mid-action leaves the page in a state nobody recorded.
        """
        self._stop.set()

    async def run(self, goal: str, *, notes: Sequence[str] = ()) -> RunResult:
        started = time.monotonic()
        result = RunResult(goal=goal)
        context = ToolContext(engine=self._engine)
        recent: list[tuple[str, str]] = []

        for index in range(self._config.max_steps):
            if self._stop.is_set():
                result.stop_reason = StopReason.STOPPED
                break
            if self._config.max_seconds and time.monotonic() - started > self._config.max_seconds:
                result.stop_reason = StopReason.BUDGET
                break

            try:
                observation = await self._observe(goal, result.steps, notes, index)
            except RelayKitError as exc:
                result.stop_reason, result.error = StopReason.ERROR, str(exc)
                break
            context.snapshot = observation.snapshot

            try:
                decision = await self._planner.decide(observation, self._tools)
            except RelayKitError as exc:
                result.stop_reason, result.error = StopReason.ERROR, str(exc)
                break

            if decision.done:
                result.answer = decision.answer
                result.stop_reason = StopReason.DONE
                result.steps.append(
                    Step(
                        decision=decision, result=ToolResult(ok=True, changed=False, summary="done")
                    )
                )
                break

            step_result = await self._act(context, decision)
            step = Step(decision=decision, result=step_result)
            result.steps.append(step)
            await self._planner.reflect(observation, step)

            # Stuck detection, keyed on *progress* rather than on repetition.
            #
            # An earlier version broke only on identical consecutive actions,
            # and a live run walked straight past it: the commonest loop is
            # act, look, act, look -- never two the same in a row, and never
            # getting anywhere. Any step that changes something clears the
            # window, so what this measures is simply "nothing has happened for
            # N actions", whatever shape they took.
            signature = (decision.tool, repr(sorted(decision.arguments.items())))
            if step_result.changed:
                recent.clear()
            else:
                recent.append(signature)
                if len(recent) >= self._config.repeat_limit:
                    tools_tried = ", ".join(sorted({name for name, _ in recent}))
                    result.stop_reason = StopReason.STUCK
                    result.error = (
                        f"{len(recent)} actions with no effect on the page (tried: {tools_tried})"
                    )
                    break
        else:
            result.stop_reason = StopReason.BUDGET

        result.elapsed = time.monotonic() - started
        # Cleared at the END, not the start: a stop requested before this run
        # began -- or between runs -- must be honoured, not discarded by the
        # very call it was meant to stop.
        self._stop.clear()
        return result

    # ------------------------------------------------------------------ #
    # Internals                                                           #
    # ------------------------------------------------------------------ #

    async def _observe(
        self, goal: str, history: Sequence[Step], notes: Sequence[str], index: int
    ) -> Observation:
        snapshot = await self._engine.snapshot()
        screenshot: Screenshot | None = None
        if self._config.include_screenshot:
            try:
                screenshot = await self._engine.screenshot()
            except RelayKitError:
                # A planner that can read a snapshot can work without pixels;
                # losing the screenshot should degrade the run, not end it.
                logger.debug("screenshot unavailable", exc_info=True)
        return Observation(
            goal=goal,
            snapshot=snapshot,
            screenshot=screenshot,
            history=tuple(history),
            notes=tuple(notes),
            step_index=index,
        )

    async def _act(self, context: ToolContext, decision: Decision) -> ToolResult:
        tool = self._by_name.get(decision.tool)
        if tool is None:
            known = ", ".join(sorted(self._by_name))
            return ToolResult.failure(f"no tool named {decision.tool!r}; available: {known}")

        # The harness raises the confirmation, not the agent. A prompt-level
        # "ask before destructive actions" rule is advisory and leaks; a gate at
        # dispatch cannot be talked out of.
        gated = tool.mutating and self._config.confirm is not None
        if gated and not self._config.confirm(decision):  # type: ignore[misc]
            return ToolResult.failure("refused: the action was not confirmed")

        try:
            return await tool.run(context, **dict(decision.arguments))
        except TypeError as exc:
            return ToolResult.failure(f"bad arguments for {decision.tool}: {exc}")
        except RelayKitError as exc:
            return ToolResult.failure(str(exc), error_type=type(exc).__name__)
        except Exception as exc:  # a third-party tool may raise anything
            logger.exception("tool %s failed", decision.tool)
            return ToolResult.failure(f"{decision.tool} raised: {exc}")


def render_history(steps: Sequence[Step], *, limit: int = 20) -> str:
    """Format history for a prompt.

    The ``[no effect]`` marker is the whole point of this function. Without it
    every line reads as progress and the model has no way to tell a working
    action from one that has been failing silently for five turns.
    """
    lines: list[str] = []
    for index, step in enumerate(steps[-limit:], start=max(1, len(steps) - limit + 1)):
        marker = "" if step.result.changed else "  [no effect]"
        if not step.result.ok:
            marker = "  [failed]"
        arguments = ", ".join(f"{k}={v!r}" for k, v in step.decision.arguments.items())
        lines.append(
            f"{index}. {step.decision.tool}({arguments}){marker}\n"
            f"   why: {step.decision.narrative}\n"
            f"   result: {step.result.summary}"
        )
    return "\n".join(lines)
