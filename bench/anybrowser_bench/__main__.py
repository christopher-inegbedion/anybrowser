"""``python -m anybrowser_bench``: grade an LLM agent on the suite."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from anybrowser import open_engine
from anybrowser.core.registry import models as model_registry

from .runner import Attempt, llm_agent, run_suite
from .tasks import ALL_TASKS, task


def _options(pairs: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair in pairs:
        key, _, value = pair.partition("=")
        lowered = value.lower()
        out[key] = lowered == "true" if lowered in {"true", "false"} else value
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m anybrowser_bench")
    parser.add_argument("--engine", default="chrome")
    parser.add_argument("-o", "--option", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--model", default="anthropic", help="Model provider.")
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--model-option", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--seeds", type=int, default=3, help="Seeds per task (0..N-1).")
    parser.add_argument("--seed-offset", type=int, default=0)
    parser.add_argument("--task", action="append", default=[], help="Repeatable. Default: all.")
    parser.add_argument("--max-steps", type=int, default=15)
    parser.add_argument("--screenshots", action="store_true")
    parser.add_argument("--json", type=Path, help="Write the full report here.")
    args = parser.parse_args(argv)

    provider = model_registry.get(args.model)(**_options(args.model_option))
    agent = llm_agent(
        provider, args.model_name, max_steps=args.max_steps, screenshots=args.screenshots
    )
    tasks = [task(t) for t in args.task] or list(ALL_TASKS)
    seeds = range(args.seed_offset, args.seed_offset + args.seeds)

    def progress(a: Attempt) -> None:
        print(f"  {a.task_id}#{a.seed}: {a.outcome} in {a.steps} steps -- {a.reason}", flush=True)

    async def go() -> int:
        async with await open_engine(args.engine, **_options(args.option)) as engine:
            report = await run_suite(agent, engine, tasks=tasks, seeds=seeds, on_attempt=progress)
        print()
        print(report.table())
        if args.json:
            args.json.write_text(report.to_json())
            print(f"report: {args.json}")
        return 0

    return asyncio.run(go())


if __name__ == "__main__":
    sys.exit(main())
