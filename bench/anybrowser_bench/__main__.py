"""``python -m anybrowser_bench``: grade an LLM agent on the suite."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from pathlib import Path
from typing import Any

from anybrowser import open_engine
from anybrowser.core.registry import models as model_registry

from .runner import Attempt, llm_agent, run_suite
from .tasks import ALL_TASKS, task


def _options(pairs: list[str]) -> dict[str, Any]:
    """KEY=VALUE pairs, coerced the way the anybrowser CLI coerces them. A
    timeout passed through as the string "300" once errored every attempt."""
    out: dict[str, Any] = {}
    for pair in pairs:
        key, _, value = pair.partition("=")
        out[key] = _coerce(value)
    return out


def _coerce(value: str) -> Any:
    lowered = value.strip().lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    for cast in (int, float):
        with contextlib.suppress(ValueError):
            return cast(value)
    return value


def _viewport_defaults(engine: str) -> dict[str, Any]:
    """Ask for 1280x800 explicitly, from any engine that takes the option.

    An engine leaves a browser it did not launch at that browser's size. A
    stale Chrome on the default port once made a run's screenshots small,
    and the vision agent's clicks all landed about 1.5x off -- with nothing
    in the report to say so. The bench's numbers are only comparable at one
    fixed size, so it asks for that size rather than trusting what it finds.
    """
    import inspect

    from anybrowser.core.registry import engines

    try:
        params = inspect.signature(engines.get(engine)).parameters
    except Exception:
        return {}
    if "viewport_width" in params and "viewport_height" in params:
        return {"viewport_width": 1280, "viewport_height": 800}
    return {}


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
    parser.add_argument(
        "--vision",
        action="store_true",
        help="Image-only agent: decides from the screenshot, clicks by coordinates.",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=2048,
        help="Per reply. Raise it for reasoning models, which think inside this budget.",
    )
    parser.add_argument("--json", type=Path, help="Write the full report here.")
    args = parser.parse_args(argv)

    provider = model_registry.get(args.model)(**_options(args.model_option))
    agent = llm_agent(
        provider,
        args.model_name,
        max_steps=args.max_steps,
        screenshots=args.screenshots,
        max_tokens=args.max_tokens,
        vision=args.vision,
    )
    tasks = [task(t) for t in args.task] or list(ALL_TASKS)
    seeds = range(args.seed_offset, args.seed_offset + args.seeds)

    def progress(a: Attempt) -> None:
        print(f"  {a.task_id}#{a.seed}: {a.outcome} in {a.steps} steps -- {a.reason}", flush=True)

    async def go() -> int:
        engine_options = {**_viewport_defaults(args.engine), **_options(args.option)}
        async with await open_engine(args.engine, **engine_options) as engine:
            report = await run_suite(agent, engine, tasks=tasks, seeds=seeds, on_attempt=progress)
        print()
        print(report.table())
        if args.json:
            args.json.write_text(report.to_json())
            print(f"report: {args.json}")
        return 0 if report.trustworthy else 2

    return asyncio.run(go())


if __name__ == "__main__":
    sys.exit(main())
