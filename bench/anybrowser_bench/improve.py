"""Self-improvement rounds: act, keep what worked, train on it, check.

    python -m anybrowser_bench.improve --out runs/qwen-vl --rounds 3

Each round, unattended:

1. **Serve** the model, plus the previous round's adapter, with mlx-vlm's
   server on localhost.
2. **Evaluate** on held-out seeds at temperature 0. Round 0's evaluation is the
   baseline. These seeds never appear in training, and the bench builds pages
   from the seed, so they are pages the model has not been trained on.
3. **Collect** on fresh training seeds at a temperature above 0, recording
   every exchange. Fresh seeds each round mean new pages, not the same page
   again.
4. **Stop the server.** On a 16 GB machine, training and serving do not fit
   together.
5. **Build** a dataset from every recording so far: judge-verified successes,
   steps that changed the page, and the final done.
6. **Train** a LoRA adapter from the base weights on that dataset. Starting
   from base each round, on the growing set, keeps an early round's mistakes
   from compounding.

There is no teacher model. The only signal is the bench's judge, which checks
the seed or the server and never takes the agent's word.

Every phase leaves a file behind, and a phase whose file exists is skipped, so
an interrupted run picks up where it stopped. ``progress.json`` holds the
numbers.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

__all__ = ["LoopConfig", "main", "run_round"]

BASE_MODEL = "mlx-community/Qwen2.5-VL-3B-Instruct-4bit"


@dataclass(slots=True)
class LoopConfig:
    out: Path
    model: str = BASE_MODEL
    rounds: int = 1
    #: Never trained on. 2 seeds x 10 tasks = 20 attempts per evaluation.
    eval_seeds: tuple[int, ...] = (1000, 1001)
    #: Fresh training seeds per round: round r uses [r*stride, r*stride + n).
    collect_seeds: int = 6
    seed_stride: int = 100
    temperature: float = 0.7
    #: Task ids to run; empty means the whole suite.
    tasks: tuple[str, ...] = ()
    max_steps: int = 10
    port: int = 8088
    chrome_port: int = 9320
    # LoRA
    learning_rate: float = 1e-4
    lora_rank: int = 8
    epochs: int = 2


# --------------------------------------------------------------------------- #
# The model server                                                             #
# --------------------------------------------------------------------------- #


@contextlib.contextmanager
def serving(config: LoopConfig, adapter: Path | None, log: Path) -> Iterator[str]:
    """Run mlx_vlm.server for the duration, on localhost only. Yields its URL."""
    _unload_ollama()
    command = [
        sys.executable,
        "-m",
        "mlx_vlm.server",
        "--host",
        "127.0.0.1",
        "--port",
        str(config.port),
        "--model",
        config.model,
    ]
    if adapter is not None:
        command += ["--adapter-path", str(adapter)]
    with log.open("a") as sink:
        process = subprocess.Popen(command, stdout=sink, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{config.port}"
    try:
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"model server exited early; see {log}")
            with contextlib.suppress(OSError):
                with urllib.request.urlopen(f"{url}/health", timeout=5) as response:
                    health = json.load(response)
                if health.get("loaded_model"):
                    wanted = str(adapter) if adapter else None
                    if wanted and not health.get("loaded_adapter"):
                        raise RuntimeError(f"server did not load the adapter {adapter}")
                    break
            time.sleep(2)
        else:
            raise RuntimeError(f"model server not healthy after 300s; see {log}")
        yield f"{url}/v1"
    finally:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _unload_ollama() -> None:
    """Free the GPU memory Ollama holds; it keeps models resident for minutes."""
    with contextlib.suppress(Exception):
        listing = subprocess.run(["ollama", "ps"], capture_output=True, text=True, timeout=10)
        for line in listing.stdout.splitlines()[1:]:
            if line.split():
                subprocess.run(["ollama", "stop", line.split()[0]], timeout=30, check=False)


# --------------------------------------------------------------------------- #
# Phases                                                                       #
# --------------------------------------------------------------------------- #


async def _bench(
    config: LoopConfig,
    base_url: str,
    seeds: Sequence[int],
    *,
    temperature: float,
    record_to: Path | None,
    log: Path,
) -> Any:
    from anybrowser import open_engine
    from anybrowser.models.openai_compatible import OpenAICompatibleProvider

    from .__main__ import _viewport_defaults
    from .runner import llm_agent, run_suite
    from .tasks import ALL_TASKS, task

    provider = OpenAICompatibleProvider(base_url=base_url, api_key="local", timeout=600)
    agent = llm_agent(
        provider,
        config.model,
        max_steps=config.max_steps,
        vision=True,
        constrained=True,
        record=record_to is not None,
        temperature=temperature,
    )
    options = {**_viewport_defaults("chrome"), "port": config.chrome_port}
    with log.open("a") as sink:

        def progress(a: Any) -> None:
            sink.write(f"{a.task_id}#{a.seed}: {a.outcome} in {a.steps} steps -- {a.reason}\n")
            sink.flush()

        async with await open_engine("chrome", **options) as engine:
            return await run_suite(
                agent,
                engine,
                tasks=[task(t) for t in config.tasks] or ALL_TASKS,
                seeds=seeds,
                on_attempt=progress,
                record_to=record_to,
            )


def _train(config: LoopConfig, dataset: Path, adapter_dir: Path, rows: int, log: Path) -> None:
    _unload_ollama()
    iters = max(1, rows * config.epochs)
    command = [
        sys.executable,
        "-m",
        "mlx_vlm.lora",
        "--model-path",
        config.model,
        "--dataset",
        str(dataset),
        "--iters",
        str(iters),
        "--batch-size",
        "1",
        "--learning-rate",
        str(config.learning_rate),
        "--lora-rank",
        str(config.lora_rank),
        "--lora-alpha",
        str(config.lora_rank * 2),
        "--max-seq-length",
        "4096",
        # Loss on the reply only: the prompt is ours, not something to learn.
        "--train-on-completions",
        # Without it the 3B model's activations at 1280x800 overflow 16 GB.
        "--grad-checkpoint",
        "--steps-per-report",
        "10",
        "--steps-per-eval",
        str(iters + 1),
        "--steps-per-save",
        str(iters + 1),
        "--output-path",
        str(adapter_dir),
    ]
    with log.open("a") as sink:
        subprocess.run(command, stdout=sink, stderr=subprocess.STDOUT, check=True)


# --------------------------------------------------------------------------- #
# A round                                                                      #
# --------------------------------------------------------------------------- #


def _write(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)  # never leave a half-written marker that looks complete


def _summary(report: Any) -> dict[str, Any]:
    by_task: dict[str, list[str]] = {}
    for a in report.attempts:
        by_task.setdefault(a.task_id, []).append(a.outcome)
    return {
        "fitness": report.fitness,
        "trustworthy": report.trustworthy,
        "success": sum(a.outcome == "success" for a in report.attempts),
        "false_claim": sum(a.outcome == "false_claim" for a in report.attempts),
        "attempts": len(report.attempts),
        "by_task": by_task,
    }


async def run_round(config: LoopConfig, r: int, adapter: Path | None) -> Path:
    """One round. Returns the adapter it trained."""
    from .dataset import build_sft

    here = config.out / f"round-{r:02d}"
    here.mkdir(parents=True, exist_ok=True)
    log = here / "log.txt"

    evaluation = here / "eval.json"
    collected = here / "collect.json"
    if not (evaluation.exists() and collected.exists()):
        with serving(config, adapter, here / "server.log") as url:
            if not evaluation.exists():
                report = await _bench(
                    config, url, config.eval_seeds, temperature=0.0, record_to=None, log=log
                )
                if not report.trustworthy:
                    raise RuntimeError(f"evaluation untrustworthy: {report.aborted or 'errors'}")
                _write(evaluation, _summary(report))
            if not collected.exists():
                start = r * config.seed_stride
                seeds = range(start, start + config.collect_seeds)
                report = await _bench(
                    config,
                    url,
                    seeds,
                    temperature=config.temperature,
                    record_to=here / "recordings",
                    log=log,
                )
                _write(collected, _summary(report))

    trained = here / "adapter"
    if not (trained / "adapters.safetensors").exists():
        dataset = here / "dataset"
        if dataset.exists():
            for f in sorted(dataset.rglob("*"), reverse=True):
                f.unlink() if f.is_file() else f.rmdir()
        recordings = sorted(config.out.glob("round-*/recordings"))
        stats = build_sft(recordings, dataset)
        _write(here / "dataset.json", asdict(stats))
        if stats.rows == 0:
            raise RuntimeError("no successful steps to train on yet")
        started = time.monotonic()
        _train(config, dataset, trained, stats.rows, log)
        _write(here / "train.json", {"rows": stats.rows, "seconds": time.monotonic() - started})
    return trained


def _progress(config: LoopConfig) -> list[dict[str, Any]]:
    rounds = []
    for here in sorted(config.out.glob("round-*")):
        entry: dict[str, Any] = {"round": here.name}
        for name in ("eval", "collect", "dataset", "train"):
            path = here / f"{name}.json"
            if path.exists():
                entry[name] = json.loads(path.read_text())
        rounds.append(entry)
    return rounds


async def run(config: LoopConfig) -> None:
    config.out.mkdir(parents=True, exist_ok=True)
    _write(config.out / "config.json", {k: str(v) for k, v in asdict(config).items()})
    adapter: Path | None = None
    for r in range(config.rounds):
        adapter = await run_round(config, r, adapter)
        _write(config.out / "progress.json", _progress(config))
    # A final evaluation of the last adapter, so the last round's training is
    # measured too, not just collected from.
    final = config.out / f"round-{config.rounds:02d}"
    final.mkdir(exist_ok=True)
    if not (final / "eval.json").exists():
        with serving(config, adapter, final / "server.log") as url:
            report = await _bench(
                config,
                url,
                config.eval_seeds,
                temperature=0.0,
                record_to=None,
                log=final / "log.txt",
            )
            _write(final / "eval.json", _summary(report))
    _write(config.out / "progress.json", _progress(config))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m anybrowser_bench.improve")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--model", default=BASE_MODEL)
    parser.add_argument("--collect-seeds", type=int, default=6)
    parser.add_argument("--eval-seeds", type=int, nargs="+", default=[1000, 1001])
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--learning-rate", type=float, default=1e-4)
    parser.add_argument("--task", action="append", default=[], help="Repeatable. Default: all.")
    args = parser.parse_args(argv)
    config = LoopConfig(
        out=args.out,
        model=args.model,
        rounds=args.rounds,
        collect_seeds=args.collect_seeds,
        eval_seeds=tuple(args.eval_seeds),
        temperature=args.temperature,
        epochs=args.epochs,
        learning_rate=args.learning_rate,
        tasks=tuple(args.task),
    )
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    asyncio.run(run(config))
    for entry in _progress(config):
        ev = entry.get("eval", {})
        print(
            f"{entry['round']}: eval fitness {ev.get('fitness', float('nan')):+.3f} "
            f"({ev.get('success', '?')}/{ev.get('attempts', '?')} success), "
            f"collected {entry.get('collect', {}).get('success', '-')} successes, "
            f"trained on {entry.get('dataset', {}).get('rows', '-')} rows"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
