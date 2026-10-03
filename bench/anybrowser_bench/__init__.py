"""A seeded, self-grading task suite for browsing agents.

The conformance suite grades engines; this grades agents. Each task draws its
answer from a seed, and the judge checks the answer against the seed or checks
what the bench server actually received. It never takes the agent's word for
it. A confident wrong answer scores -1, below an honest failure at 0, so the
fitness number cannot be raised by guessing.

    from anybrowser_bench import llm_agent, run_suite

    async with await open_engine("chrome") as engine:
        report = await run_suite(llm_agent(provider, "some-model"), engine)
    print(report.table())

Or from a shell: ``python -m anybrowser_bench --help``.
"""

from .runner import Agent, Attempt, Report, llm_agent, run_suite
from .server import BenchServer
from .task import SCORES, Instance, Kind, Outcome, Submission, Task, Verdict, judge
from .tasks import ALL_TASKS, task

__all__ = [
    "ALL_TASKS",
    "SCORES",
    "Agent",
    "Attempt",
    "BenchServer",
    "Instance",
    "Kind",
    "Outcome",
    "Report",
    "Submission",
    "Task",
    "Verdict",
    "judge",
    "llm_agent",
    "run_suite",
    "task",
]
