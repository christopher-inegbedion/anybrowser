"""Run an agent across the suite and reduce it to a fitness number.

An *agent* here is anything that can be handed an engine and a goal and return
an :class:`~anybrowser.agent.RunResult`. That is deliberately not tied to
:class:`~anybrowser.agent.AgentRunner`: the point of a bench is to compare
agents that differ in their planner, their tools, their memory or their whole
loop, and every one of them must be graded the same way.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from statistics import mean
from typing import Any

from anybrowser.agent import RunResult, StopReason
from anybrowser.core.engine import BrowserEngine

from .server import BenchServer
from .task import Outcome, Task, judge
from .tasks import ALL_TASKS

__all__ = ["Agent", "Attempt", "Report", "llm_agent", "run_suite"]

Agent = Callable[[BrowserEngine, str], Awaitable[RunResult]]


@dataclass(frozen=True, slots=True)
class Attempt:
    task_id: str
    seed: int
    kind: str
    outcome: str
    score: float
    reason: str
    stop_reason: str
    steps: int
    seconds: float
    answer: str
    tools: tuple[str, ...]


@dataclass(slots=True)
class Report:
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def fitness(self) -> float:
        """Mean score in [-1, 1]. 1 is perfect; below 0 means it lies more
        than it succeeds."""
        return mean(a.score for a in self.attempts) if self.attempts else 0.0

    def count(self, outcome: Outcome) -> int:
        return sum(a.outcome == outcome.value for a in self.attempts)

    def by_task(self) -> dict[str, list[Attempt]]:
        grouped: dict[str, list[Attempt]] = {}
        for a in self.attempts:
            grouped.setdefault(a.task_id, []).append(a)
        return grouped

    def to_json(self) -> str:
        return json.dumps(
            {
                "fitness": self.fitness,
                "success": self.count(Outcome.SUCCESS),
                "failed": self.count(Outcome.FAILED),
                "false_claim": self.count(Outcome.FALSE_CLAIM),
                "attempts": [asdict(a) for a in self.attempts],
            },
            indent=2,
        )

    def table(self) -> str:
        lines = [f"{'task':<16} {'score':>6}  outcomes"]
        for task_id, attempts in self.by_task().items():
            marks = " ".join(_MARK[a.outcome] for a in attempts)
            lines.append(f"{task_id:<16} {mean(a.score for a in attempts):>6.2f}  {marks}")
        lines.append(
            f"\nfitness {self.fitness:+.3f}   "
            f"{self.count(Outcome.SUCCESS)} success, {self.count(Outcome.FAILED)} failed, "
            f"{self.count(Outcome.FALSE_CLAIM)} false claim"
        )
        return "\n".join(lines)


_MARK = {"success": "✓", "failed": "·", "false_claim": "✗"}


async def run_suite(
    agent: Agent,
    engine: BrowserEngine,
    *,
    tasks: Iterable[Task] = ALL_TASKS,
    seeds: Sequence[int] = (0, 1, 2),
    on_attempt: Callable[[Attempt], None] | None = None,
) -> Report:
    """Attempt every task at every seed, sequentially, on one engine."""
    report = Report()
    with BenchServer() as server:
        for task in tasks:
            for seed in seeds:
                attempt = await _attempt(agent, engine, server, task, seed)
                report.attempts.append(attempt)
                if on_attempt:
                    on_attempt(attempt)
    return report


async def _attempt(
    agent: Agent, engine: BrowserEngine, server: BenchServer, task: Task, seed: int
) -> Attempt:
    instance = task.build(seed)
    root = server.mount(instance)
    started = time.monotonic()
    await engine.navigate(root + instance.start)
    try:
        result = await agent(engine, instance.goal)
    except Exception as exc:  # an agent under evolution may raise anything
        result = RunResult(goal=instance.goal, stop_reason=StopReason.ERROR, error=repr(exc))
    verdict = judge(
        instance,
        finished=result.stop_reason == StopReason.DONE,
        answer=result.answer,
        submissions=server.submissions(root),
    )
    return Attempt(
        task_id=task.id,
        seed=seed,
        kind=task.kind.value,
        outcome=verdict.outcome.value,
        score=verdict.score,
        reason=verdict.reason if not result.error else f"{verdict.reason} ({result.error})",
        stop_reason=str(result.stop_reason),
        steps=result.step_count,
        seconds=round(time.monotonic() - started, 2),
        answer=result.answer,
        tools=tuple(s.decision.tool for s in result.steps if s.decision.tool),
    )


def llm_agent(
    provider: Any, model: str, *, max_steps: int = 15, screenshots: bool = False
) -> Agent:
    """The reference agent: :class:`AgentRunner` with an :class:`LLMPlanner`.

    A fresh planner per attempt, so nothing leaks from one task into the next.
    Screenshots default to off: every task here is solvable from the page text,
    and images multiply the cost of a suite run. Turn them on to grade the
    agent as the CLI runs it.
    """
    from anybrowser.agent import AgentRunner, LLMPlanner, RunConfig

    async def run(engine: BrowserEngine, goal: str) -> RunResult:
        planner = LLMPlanner(provider, model=model)
        config = RunConfig(max_steps=max_steps, include_screenshot=screenshots)
        return await AgentRunner(engine, planner, config=config).run(goal)

    return run
