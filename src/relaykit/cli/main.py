"""The ``relaykit`` command.

Small on purpose. It exists so the library can be exercised without writing a
script -- see what engines exist, look at a page, run the daemon, drive an
agent -- and every command is a thin wrapper over the same public API a caller
would use.

Uses argparse rather than a CLI framework so the base install stays
dependency-free: ``import relaykit`` must not pull in a click/typer tree.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from collections.abc import Sequence
from typing import Any

from .. import __version__, available_engines, open_engine
from ..core.errors import RelayKitError
from ..core.registry import models as model_registry
from ..core.registry import transports as transport_registry

__all__ = ["build_parser", "main"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="relaykit",
        description="A pluggable browser-automation and agent runtime.",
    )
    parser.add_argument("--version", action="version", version=f"relaykit {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    plugins = sub.add_parser("plugins", help="List installed engines, transports and models.")
    plugins.add_argument("--json", action="store_true", help="Machine-readable output.")

    def add_engine_options(p: argparse.ArgumentParser) -> None:
        p.add_argument("--engine", default="chrome", help="Engine to use (default: chrome).")
        p.add_argument(
            "-o",
            "--option",
            action="append",
            default=[],
            metavar="KEY=VALUE",
            help="Engine constructor option. Repeatable.",
        )

    info = sub.add_parser("info", help="What an engine is and what it can do.")
    add_engine_options(info)

    look = sub.add_parser("look", help="Open a URL and print what is on the page.")
    add_engine_options(look)
    look.add_argument("url")
    look.add_argument("--limit", type=int, default=40, help="Max elements to print.")
    look.add_argument("--json", action="store_true")
    look.add_argument("--screenshot", metavar="PATH", help="Also save a PNG here.")

    serve = sub.add_parser("serve", help="Run a daemon serving one browser.")
    add_engine_options(serve)
    serve.add_argument("--transport", default="unix", help="Transport (default: unix).")
    serve.add_argument("--address", default="", help="Bind address or socket path.")
    serve.add_argument("--token", default="", help="Require this token from clients.")

    run = sub.add_parser("run", help="Give an agent a goal and let it work.")
    add_engine_options(run)
    run.add_argument("goal")
    run.add_argument("--model", default="anthropic", help="Model provider (default: anthropic).")
    run.add_argument("--model-name", default="claude-sonnet-5", help="Model id.")
    run.add_argument("--max-steps", type=int, default=25)
    run.add_argument(
        "--confirm",
        action="store_true",
        help="Ask before every action that changes the page.",
    )

    return parser


def _parse_options(pairs: Sequence[str]) -> dict[str, Any]:
    options: dict[str, Any] = {}
    for raw in pairs:
        key, _, value = raw.partition("=")
        if not key or not _:
            raise SystemExit(f"malformed --option {raw!r}; expected KEY=VALUE")
        options[key.strip()] = _coerce(value)
    return options


def _coerce(value: str) -> Any:
    lowered = value.strip().lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    for cast in (int, float):
        with contextlib.suppress(ValueError):
            return cast(value)
    return value


# --------------------------------------------------------------------------- #
# Commands                                                                     #
# --------------------------------------------------------------------------- #


def cmd_plugins(args: argparse.Namespace) -> int:
    found = {
        "engines": available_engines(),
        "transports": transport_registry.names(),
        "models": model_registry.names(),
    }
    if args.json:
        print(json.dumps(found, indent=2))
        return 0
    for group, names in found.items():
        print(f"{group}:")
        for name in names:
            print(f"  {name}")
        if not names:
            print("  (none installed)")
    return 0


async def cmd_info(args: argparse.Namespace) -> int:
    async with await open_engine(args.engine, **_parse_options(args.option)) as engine:
        info = await engine.info()
        print(f"{info.name}: {info.browser} {info.browser_version}".rstrip())
        print(f"platform: {info.platform}")
        print("capabilities:")
        caps = engine.capabilities
        for capability in sorted(caps.supported, key=lambda c: c.value):
            note = caps.notes.get(capability, "")
            print(f"  {capability.value}" + (f" — {note}" if note else ""))
        if not caps.supported:
            print("  (none declared)")
    return 0


async def cmd_look(args: argparse.Namespace) -> int:
    async with await open_engine(args.engine, **_parse_options(args.option)) as engine:
        result = await engine.navigate(args.url)
        if not result.ok:
            print(f"could not open {args.url}: {result.error}", file=sys.stderr)
            return 1
        page = await engine.snapshot()

        if args.screenshot:
            shot = await engine.screenshot()
            with open(args.screenshot, "wb") as handle:
                handle.write(shot.data)

        if args.json:
            from ..daemon import codec

            print(json.dumps(codec.dump_snapshot(page), indent=2))
            return 0

        print(f"{page.title}\n{page.url}\n")
        print(f"{len(page.elements)} interactive element(s):")
        for element in page.elements[: args.limit]:
            centre = element.box.center
            print(
                f"  [{element.handle}] {element.tag:<8} {element.description[:60]!r}"
                f"  @({int(centre.x)},{int(centre.y)})"
            )
        if len(page.elements) > args.limit:
            print(f"  … and {len(page.elements) - args.limit} more")
        if args.screenshot:
            print(f"\nscreenshot: {args.screenshot}")
    return 0


async def cmd_serve(args: argparse.Namespace) -> int:
    from ..daemon import DaemonServer, TokenAuth

    transport_cls = transport_registry.get(args.transport)
    transport = transport_cls(args.address) if args.address else transport_cls()
    engine = await open_engine(args.engine, **_parse_options(args.option))
    auth = TokenAuth(args.token) if args.token else None
    server = DaemonServer(engine, transport, auth=auth)

    print(f"serving {args.engine} on {transport.address}")
    if not args.token and args.transport != "unix":
        # Worth saying out loud: this is complete control of a browser holding
        # live logins, and only the Unix socket has the filesystem to guard it.
        print("warning: no --token on a network transport", file=sys.stderr)
    try:
        await server.serve()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await server.close()
        await engine.close()
    return 0


async def cmd_run(args: argparse.Namespace) -> int:
    from ..agent import AgentRunner, LLMPlanner, RunConfig

    provider = model_registry.get(args.model)()
    planner = LLMPlanner(provider, model=args.model_name)

    def confirm(decision: Any) -> bool:
        answer = input(f"  run {decision.tool}({decision.arguments})? [y/N] ")
        return answer.strip().lower() in {"y", "yes"}

    config = RunConfig(max_steps=args.max_steps, confirm=confirm if args.confirm else None)

    async with await open_engine(args.engine, **_parse_options(args.option)) as engine:
        runner = AgentRunner(engine, planner, config=config)
        result = await runner.run(args.goal)

    for index, step in enumerate(result.steps, start=1):
        marker = "" if step.result.changed else "  [no effect]"
        if not step.result.ok:
            marker = "  [failed]"
        print(f"{index}. {step.decision.tool}{marker}")
        print(f"   {step.decision.narrative}")
        print(f"   → {step.result.summary}")

    print(f"\n{result.stop_reason} after {result.step_count} step(s), {result.elapsed:.1f}s")
    if planner.total_cost_usd:
        print(f"cost: ${planner.total_cost_usd:.4f} ({planner.total_tokens} tokens)")
    if result.answer:
        print(f"\n{result.answer}")
    if result.error:
        print(f"\nerror: {result.error}", file=sys.stderr)
    return 0 if result.ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers: dict[str, Any] = {
        "plugins": cmd_plugins,
        "info": cmd_info,
        "look": cmd_look,
        "serve": cmd_serve,
        "run": cmd_run,
    }
    handler = handlers[args.command]
    try:
        if asyncio.iscoroutinefunction(handler):
            return int(asyncio.run(handler(args)))
        return int(handler(args))
    except RelayKitError as exc:
        # A typed failure is a message for the user, not a traceback.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
