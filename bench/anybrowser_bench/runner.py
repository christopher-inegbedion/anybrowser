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
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from statistics import mean
from typing import Any

from anybrowser.agent import RunResult, StopReason
from anybrowser.core.engine import BrowserEngine
from anybrowser.models.provider import ModelProvider

from .record import Exchange, RecordedRun, RecordingProvider, SeedingProvider, write_attempt
from .server import BenchServer
from .task import Instance, Outcome, Task, judge
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
    #: Every step: what was chosen, why, and what the engine said happened.
    #: The raw material for diagnosing an agent, and for training one -- a
    #: step that changed the page is a different signal from one that did not,
    #: long before the task's own outcome is known.
    trace: tuple[Mapping[str, Any], ...] = ()

    @property
    def scored(self) -> bool:
        return self.outcome != Outcome.ERROR.value


@dataclass(slots=True)
class Report:
    attempts: list[Attempt] = field(default_factory=list)
    #: Why the suite stopped early, if it did.
    aborted: str = ""

    @property
    def fitness(self) -> float:
        """Mean score in [-1, 1] over the attempts that say something about the
        agent. 1 is perfect; below 0 means it is wrong more often than right."""
        scored = [a.score for a in self.attempts if a.scored]
        return mean(scored) if scored else 0.0

    @property
    def trustworthy(self) -> bool:
        """False when errors or an early stop mean the fitness is not comparable
        to a clean run's. An evolution loop should discard such a report, not
        rank it."""
        return not self.aborted and self.count(Outcome.ERROR) == 0

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
                "trustworthy": self.trustworthy,
                "aborted": self.aborted,
                "error": self.count(Outcome.ERROR),
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
            scored = [a.score for a in attempts if a.scored]
            score = f"{mean(scored):>6.2f}" if scored else f"{'-':>6}"
            lines.append(f"{task_id:<16} {score}  {marks}")
        lines.append(
            f"\nfitness {self.fitness:+.3f}   "
            f"{self.count(Outcome.SUCCESS)} success, {self.count(Outcome.FAILED)} failed, "
            f"{self.count(Outcome.FALSE_CLAIM)} false claim, {self.count(Outcome.ERROR)} error"
        )
        if not self.trustworthy:
            why = self.aborted or "some attempts errored"
            lines.append(f"NOT TRUSTWORTHY: {why}. Do not compare this fitness.")
        return "\n".join(lines)


#: Errors that are the agent's fault, however the loop reports them. A model
#: whose reply cannot be parsed has failed the task; the first baseline counted
#: that as infrastructure and excluded a weak model's every failure.
_AGENT_ERRORS = frozenset({"ModelOutputError"})

_MARK = {"success": "✓", "failed": "·", "false_claim": "✗", "error": "!"}


async def run_suite(
    agent: Agent,
    engine: BrowserEngine,
    *,
    tasks: Iterable[Task] = ALL_TASKS,
    seeds: Sequence[int] = (0, 1, 2),
    on_attempt: Callable[[Attempt], None] | None = None,
    max_consecutive_errors: int = 3,
    record_to: Path | None = None,
    repeats: int = 1,
) -> Report:
    """Attempt every task at every seed, sequentially, on one engine.

    Stops after ``max_consecutive_errors`` errors in a row: by then the model
    server or the browser is down, and every further attempt costs time to
    learn nothing.

    ``repeats`` attempts every (task, seed) that many times. Repeats start from
    the same page, so with a temperature above 0 they are different replies to
    an identical state -- the raw material for preference pairs.

    ``record_to`` writes each attempt's model exchanges there, for an agent
    that returns a :class:`~anybrowser_bench.record.RecordedRun` (``llm_agent``
    with ``record=True``). See :mod:`anybrowser_bench.record`.
    """
    report = Report()
    streak = 0
    with BenchServer() as server:
        for task in tasks:
            for seed in [s for s in seeds for _ in range(max(1, repeats))]:
                attempt, exchanges = await _attempt(agent, engine, server, task, seed)
                report.attempts.append(attempt)
                if record_to is not None and exchanges:
                    write_attempt(record_to, attempt, exchanges)
                if on_attempt:
                    on_attempt(attempt)
                streak = streak + 1 if not attempt.scored else 0
                if max_consecutive_errors and streak >= max_consecutive_errors:
                    report.aborted = f"{streak} errors in a row, last: {attempt.reason}"
                    return report
    return report


async def _attempt(
    agent: Agent, engine: BrowserEngine, server: BenchServer, task: Task, seed: int
) -> tuple[Attempt, list[Exchange]]:
    instance = task.build(seed)
    root = server.mount(instance)
    started = time.monotonic()
    try:
        await engine.navigate(root + instance.start)
    except Exception as exc:
        # The browser, not the agent: a dropped DevTools connection once
        # crashed a whole unattended run here, four hours in. As an error it
        # is unscored, and three in a row stop the suite cleanly.
        result = RunResult(
            goal=instance.goal,
            stop_reason=StopReason.ERROR,
            error=f"could not open the task page: {exc!r}",
            error_type=type(exc).__name__,
        )
        return _attempt_record(task, seed, started, result, None, server, root), []
    try:
        result = await agent(engine, instance.goal)
    except Exception as exc:
        # The agent's own code raised. Under evolution that is the agent's
        # fault, and scored as a failure -- unlike StopReason.ERROR below, which
        # the loop reports when the model or the engine broke underneath it.
        result = RunResult(goal=instance.goal, stop_reason="agent_raised", error=repr(exc))
    attempt = _attempt_record(task, seed, started, result, instance, server, root)
    return attempt, list(getattr(result, "exchanges", ()))


def _attempt_record(
    task: Task,
    seed: int,
    started: float,
    result: RunResult,
    instance: Instance | None,
    server: BenchServer,
    root: str,
) -> Attempt:
    if result.stop_reason == StopReason.ERROR and result.error_type not in _AGENT_ERRORS:
        outcome, score, reason = Outcome.ERROR.value, 0.0, f"infrastructure: {result.error}"
    else:
        assert instance is not None, "only a page that opened can be judged"
        verdict = judge(
            instance,
            finished=result.stop_reason == StopReason.DONE,
            answer=result.answer,
            submissions=server.submissions(root),
        )
        outcome, score = verdict.outcome.value, verdict.score
        reason = verdict.reason if not result.error else f"{verdict.reason} ({result.error})"
    return Attempt(
        task_id=task.id,
        seed=seed,
        kind=task.kind.value,
        outcome=outcome,
        score=score,
        reason=reason,
        stop_reason=str(result.stop_reason),
        steps=result.step_count,
        seconds=round(time.monotonic() - started, 2),
        answer=result.answer,
        tools=tuple(s.decision.tool for s in result.steps if s.decision.tool),
        trace=tuple(
            {
                "tool": s.decision.tool or "done",
                "arguments": dict(s.decision.arguments),
                "narrative": s.decision.narrative,
                "ok": s.result.ok,
                "changed": s.result.changed,
                "summary": s.result.summary,
            }
            for s in result.steps
        ),
    )


def llm_agent(
    provider: Any,
    model: str,
    *,
    max_steps: int = 15,
    screenshots: bool = False,
    max_tokens: int = 2048,
    vision: bool = False,
    constrained: bool = False,
    record: bool = False,
    temperature: float = 0.0,
    seed: int = 0,
) -> Agent:
    """The reference agent: :class:`AgentRunner` with an :class:`LLMPlanner`.

    A fresh planner per attempt, so nothing leaks from one task into the next.
    Screenshots default to off: every task here is solvable from the page text,
    and images multiply the cost of a suite run. Turn them on to grade the
    agent as the CLI runs it. A reasoning model needs ``max_tokens`` well
    above the default, or it can spend the budget thinking and never reply.

    ``vision`` runs the image-only agent instead: :class:`VisionPlanner` with
    coordinate tools, deciding from the screenshot alone. ``constrained`` has
    the provider enforce the reply's shape (ADR-0009). ``record`` keeps every
    model exchange verbatim for training; ``temperature`` above 0 makes repeated
    attempts differ, which is what collecting successes to learn from needs.
    """
    from anybrowser.agent import AgentRunner, LLMPlanner, RunConfig, VisionPlanner, vision_tools

    # Above temperature 0, every request carries its own seed, counting up from
    # ``seed``: a server that fixes its sampling seed otherwise returns the same
    # "sample" every time (see SeedingProvider).
    sampled: ModelProvider = SeedingProvider(provider, seed) if temperature > 0 else provider

    async def run(engine: BrowserEngine, goal: str) -> RunResult:
        source = RecordingProvider(sampled) if record else sampled
        if vision:
            planner: LLMPlanner = VisionPlanner(
                source,
                model=model,
                max_tokens=max_tokens,
                constrained=constrained,
                temperature=temperature,
            )
            config = RunConfig(max_steps=max_steps, include_screenshot=True)
            runner = AgentRunner(engine, planner, tools=vision_tools(), config=config)
        else:
            planner = LLMPlanner(
                source,
                model=model,
                max_tokens=max_tokens,
                constrained=constrained,
                temperature=temperature,
            )
            config = RunConfig(max_steps=max_steps, include_screenshot=screenshots)
            runner = AgentRunner(engine, planner, config=config)
        result = await runner.run(goal)
        if isinstance(source, RecordingProvider):
            recorded = RecordedRun(**{f: getattr(result, f) for f in _RUN_FIELDS})
            recorded.exchanges = source.exchanges
            return recorded
        return result

    return run


_RUN_FIELDS = tuple(f.name for f in fields(RunResult))
