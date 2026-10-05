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
import shutil
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
    #: Never trained on. 5 seeds x 10 tasks = 50 attempts per evaluation: with
    #: 3, single tasks swung 0/3 <-> 3/3 between rounds and the trend was noise.
    eval_seeds: tuple[int, ...] = (1000, 1001, 1002, 1003, 1004)
    #: Fresh training seeds per round: round r uses [r*stride, r*stride + n).
    collect_seeds: int = 3
    #: Attempts per training seed. Repeats share their first state, so their
    #: differing replies at it become preference pairs.
    repeats: int = 3
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
    #: "privileged" trains on pairs labelled by what the bench knew at each step,
    #: plus hindsight and outcome pairs (dataset.build_privileged); "orpo" on
    #: outcome pairs only; "sft" on successes only. ORPO-style modes fall back
    #: to rebalanced SFT when they find no pairs.
    train_mode: str = "privileged"
    #: Pairs per round in privileged mode. An ORPO step is ~48s here.
    max_pairs: int = 60
    #: The adapter round 0 starts from (and is evaluated as); None is the base.
    start_adapter: Path | None = None
    #: Keep the adapter with the best held-out fitness, collect with it, and
    #: continue training from it rather than from the base. Retraining from the
    #: base each round let every round land somewhere new: fitness went
    #: +0.32 -> +0.10 between the last two rounds of the run before this.
    continue_from_best: bool = True
    #: When continuing, train on the last N rounds' recordings, not all of them,
    #: so pairs already learnt are not re-learnt into the ground every round.
    data_window: int = 2
    continue_learning_rate: float = 5e-5
    #: "rl": policy gradient on every recorded step (anybrowser_bench.rl_train),
    #: advantages centred per task. Needs a start adapter.
    rl_max_samples: int = 150
    rl_learning_rate: float = 1e-5
    label_weight: float = 0.5
    #: Evaluate a candidate on this many held-out seeds first, and stop there if
    #: it is already behind the best on those same seeds. 0 disables.
    screen_seeds: int = 3
    #: The vision agent's compact prompt (VisionPlanner(compact=True)).
    compact: bool = False
    #: mlx-vlm's automatic prefix cache. Off: in 0.7.4 its self-check fails for
    #: Qwen2.5-VL ("model has no make_cache"), so it saved nothing, measured
    #: 8.2s/step against 8.0s without it -- while still opening a disk tier
    #: allowed to grow to 20 GB under ~/.cache/mlx-vlm/apc.
    prefix_cache: bool = False
    beta: float = 0.1
    max_pairs_per_task: int = 10
    #: An ORPO step on this machine is ~48s (two sequences, uncompiled); this
    #: bounds a round's training to ~1.6h however many pairs there are.
    max_iters: int = 120
    #: SFT rebalancing (see dataset._rebalance).
    max_per_task: int = 8
    max_done_share: float = 0.5


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
    env = {**os.environ, "APC_ENABLED": "1" if config.prefix_cache else "0"}
    with log.open("a") as sink:
        process = subprocess.Popen(command, stdout=sink, stderr=subprocess.STDOUT, env=env)
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
    repeats: int = 1,
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
        compact=config.compact,
        # Distinct per round, so a resumed or later round never replays the
        # same sampling sequence.
        seed=int(time.time()),
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
                repeats=repeats,
            )


def _train(
    config: LoopConfig,
    dataset: Path,
    adapter_dir: Path,
    rows: int,
    log: Path,
    mode: str,
    init: Path | None = None,
) -> None:
    _unload_ollama()
    iters = max(1, min(rows * config.epochs, config.max_iters))
    command = [
        sys.executable,
        "-m",
        "anybrowser_bench.mlx_train",
        "--model-path",
        config.model,
        "--dataset",
        str(dataset),
        "--iters",
        str(iters),
        "--batch-size",
        "1",
        "--learning-rate",
        str(config.continue_learning_rate if init is not None else config.learning_rate),
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
    if mode in ("orpo", "privileged"):
        command += ["--train-mode", "orpo", "--beta", str(config.beta)]
    if init is not None:
        command += ["--adapter-path", str(init)]
    with log.open("a") as sink:
        subprocess.run(command, stdout=sink, stderr=subprocess.STDOUT, check=True)
    config_file = adapter_dir / "adapter_config.json"
    if init is not None and not config_file.exists():
        # Resuming from --adapter-path, mlx-vlm 0.7.4 writes the weights but
        # not adapter_config.json, and the server then cannot load the result.
        # The architecture is the starting adapter's, so its config applies.
        shutil.copyfile(init / "adapter_config.json", config_file)


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


def _train_rl(
    config: LoopConfig, recordings: list[Path], out: Path, init: Path, log: Path
) -> dict[str, Any]:
    _unload_ollama()
    command = [
        sys.executable,
        "-m",
        "anybrowser_bench.rl_train",
        *[str(r) for r in recordings],
        "--init",
        str(init),
        "--out",
        str(out),
        "--model",
        config.model,
        "--max-samples",
        str(config.rl_max_samples),
        "--learning-rate",
        str(config.rl_learning_rate),
        "--label-weight",
        str(config.label_weight),
    ]
    with log.open("a") as sink:
        subprocess.run(command, stdout=sink, stderr=subprocess.STDOUT, check=True)
    return dict(json.loads((out / "rl_train.json").read_text()))


def _best(config: LoopConfig) -> tuple[float, Path | None]:
    path = config.out / "best.json"
    if not path.exists():
        return float("-inf"), None
    data = json.loads(path.read_text())
    return float(data["fitness"]), Path(data["adapter"]) if data["adapter"] else None


def _consider(config: LoopConfig, r: int, adapter: Path | None) -> None:
    """Selection: remember the adapter if its held-out fitness is the best yet."""
    evaluation = config.out / f"round-{r:02d}" / "eval.json"
    if not evaluation.exists():
        return
    data = json.loads(evaluation.read_text())
    if data.get("screened_out"):
        return  # scored on fewer seeds; not comparable, and behind anyway
    fitness = float(data["fitness"])
    if fitness > _best(config)[0]:
        _write(
            config.out / "best.json",
            {"fitness": fitness, "adapter": str(adapter) if adapter else None, "round": r},
        )


def _best_screen(config: LoopConfig) -> float | None:
    """The best adapter's mean score on the screening seeds, from its own eval."""
    path = config.out / "best.json"
    if not config.screen_seeds or not path.exists():
        return None
    best_round = json.loads(path.read_text())["round"]
    evaluation = config.out / f"round-{best_round:02d}" / "eval.json"
    if not evaluation.exists():
        return None
    by_task = json.loads(evaluation.read_text())["by_task"]
    from .task import SCORES, Outcome

    scores = [
        SCORES[Outcome(o)]
        for outcomes in by_task.values()
        for o in outcomes[: config.screen_seeds]
        if o != Outcome.ERROR.value
    ]
    return sum(scores) / len(scores) if scores else None


async def _evaluate(config: LoopConfig, here: Path, adapter: Path | None) -> None:
    from .runner import Report

    evaluation = here / "eval.json"
    if evaluation.exists():
        return
    seeds = list(config.eval_seeds)
    bar = _best_screen(config)
    first = seeds[: config.screen_seeds] if bar is not None else seeds
    with serving(config, adapter, here / "server.log") as url:
        # Recorded so a regression can be read rather than guessed at. Never
        # trained on: datasets read round-*/recordings only.
        async def play(chunk: list[int]) -> Any:
            report = await _bench(
                config,
                url,
                chunk,
                temperature=0.0,
                record_to=here / "eval-recordings",
                log=here / "log.txt",
            )
            if not report.trustworthy:
                raise RuntimeError(f"evaluation untrustworthy: {report.aborted or 'errors'}")
            return report

        report = await play(first)
        if bar is not None and report.fitness < bar:
            # Behind the best on the same seeds already: the remaining seeds
            # could only confirm a rejection. Recorded, never selected.
            _write(evaluation, {**_summary(report), "screened_out": True, "bar": bar})
            return
        if len(first) < len(seeds):
            rest = await play(seeds[len(first) :])
            # Seed order within each task must match a full run's, so the
            # screening bar keeps reading the same seeds.
            merged = Report(attempts=report.attempts + rest.attempts)
            merged.attempts.sort(key=lambda a: (list(_task_ids(config)).index(a.task_id), a.seed))
            report = merged
    _write(evaluation, _summary(report))


def _task_ids(config: LoopConfig) -> list[str]:
    from .tasks import ALL_TASKS

    return list(config.tasks) or [t.id for t in ALL_TASKS]


async def run_round(config: LoopConfig, r: int, adapter: Path | None) -> Path:
    """Evaluate ``adapter``; collect with the best adapter so far; train from it.

    Returns the adapter this round trained, which the next round evaluates.
    """
    from .dataset import build_preferences, build_privileged, build_sft

    here = config.out / f"round-{r:02d}"
    here.mkdir(parents=True, exist_ok=True)
    log = here / "log.txt"

    await _evaluate(config, here, adapter)
    _consider(config, r, adapter)
    best = _best(config)[1] if config.continue_from_best else adapter

    collected = here / "collect.json"
    if not collected.exists():
        with serving(config, best, here / "collect-server.log") as url:
            start = r * config.seed_stride
            seeds = range(start, start + config.collect_seeds)
            report = await _bench(
                config,
                url,
                seeds,
                temperature=config.temperature,
                record_to=here / "recordings",
                log=log,
                repeats=config.repeats,
            )
        if report.aborted:
            # A browser or server that died mid-collection: stop here, so a
            # rerun collects again rather than training on a fraction of the
            # round. What was recorded is kept.
            raise RuntimeError(f"collection stopped: {report.aborted}")
        _write(collected, _summary(report))

    trained = here / "adapter"
    if not (trained / "adapters.safetensors").exists():
        dataset = here / "dataset"
        if dataset.exists():
            for f in sorted(dataset.rglob("*"), reverse=True):
                f.unlink() if f.is_file() else f.rmdir()
        recordings = sorted(config.out.glob("round-*/recordings"))
        if config.continue_from_best and config.data_window > 0:
            recordings = recordings[-config.data_window :]
        mode, rows = config.train_mode, 0
        if mode == "rl":
            if best is None:
                raise RuntimeError("rl mode continues from an adapter: pass --start-adapter")
            started = time.monotonic()
            rl_stats = _train_rl(config, recordings, trained, best, log)
            _write(here / "dataset.json", {"mode": mode, "samples": rl_stats["samples"]})
            _write(
                here / "train.json",
                {
                    "mode": mode,
                    "rows": rl_stats["samples"],
                    "init": str(best),
                    "seconds": time.monotonic() - started,
                    **rl_stats,
                },
            )
            return trained
        if mode == "privileged":
            vstats = build_privileged(recordings, dataset, max_pairs=config.max_pairs)
            _write(here / "dataset.json", {"mode": mode, **asdict(vstats)})
            rows = vstats.pairs
        elif mode == "orpo":
            pstats = build_preferences(
                recordings, dataset, max_pairs_per_task=config.max_pairs_per_task
            )
            _write(here / "dataset.json", {"mode": mode, **asdict(pstats)})
            rows = pstats.pairs
        if mode != "sft" and rows == 0:
            for f in sorted(dataset.rglob("*"), reverse=True):
                f.unlink() if f.is_file() else f.rmdir()
            mode = "sft"
        if mode == "sft":
            stats = build_sft(
                recordings,
                dataset,
                max_per_task=config.max_per_task,
                max_done_share=config.max_done_share,
            )
            _write(here / "dataset.json", {"mode": "sft", **asdict(stats)})
            rows = stats.rows
        if rows == 0:
            raise RuntimeError("nothing to train on yet")
        started = time.monotonic()
        init = best if config.continue_from_best else None
        _train(config, dataset, trained, rows, log, mode, init=init)
        _write(
            here / "train.json",
            {
                "mode": mode,
                "rows": rows,
                "init": str(init) if init else None,
                "seconds": time.monotonic() - started,
            },
        )
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
    adapter: Path | None = config.start_adapter
    for r in range(config.rounds):
        adapter = await run_round(config, r, adapter)
        _write(config.out / "progress.json", _progress(config))
    # A final evaluation of the last adapter, so the last round's training is
    # measured too, not just collected from -- and can still be selected.
    final = config.out / f"round-{config.rounds:02d}"
    final.mkdir(exist_ok=True)
    await _evaluate(config, final, adapter)
    _consider(config, config.rounds, adapter)
    _write(config.out / "progress.json", _progress(config))


def _stay_awake() -> None:
    """Keep a Mac from idle-sleeping for as long as this process lives.

    A run is hours long and unattended, and a Mac set to sleep after a minute
    of idleness suspends the GPU, the model server and Chrome along with it.
    The first overnight run looked like a training stall -- 2% CPU, no step in
    1h45m -- until the power log showed "Wake from Deep Idle". ``caffeinate -w``
    ties the assertion to this pid, so it ends when the run does, however it
    ends. Elsewhere, a no-op.
    """
    if sys.platform != "darwin":
        return
    with contextlib.suppress(OSError):
        subprocess.Popen(
            ["caffeinate", "-i", "-w", str(os.getpid())],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m anybrowser_bench.improve")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--model", default=BASE_MODEL)
    parser.add_argument("--collect-seeds", type=int, default=3)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--eval-seeds", type=int, nargs="+", default=[1000, 1001, 1002, 1003, 1004])
    parser.add_argument(
        "--train-mode", choices=["rl", "privileged", "orpo", "sft"], default="privileged"
    )
    parser.add_argument("--compact", action="store_true", help="Compact vision prompt.")
    parser.add_argument("--rl-samples", type=int, default=150)
    parser.add_argument("--screen-seeds", type=int, default=3)
    parser.add_argument("--start-adapter", type=Path, default=None)
    parser.add_argument(
        "--from-base",
        action="store_true",
        help="Train each round from the base weights on all data (the earlier method).",
    )
    parser.add_argument("--max-pairs", type=int, default=60)
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
        repeats=args.repeats,
        train_mode=args.train_mode,
        start_adapter=args.start_adapter,
        continue_from_best=not args.from_base,
        max_pairs=args.max_pairs,
        compact=args.compact,
        rl_max_samples=args.rl_samples,
        screen_seeds=args.screen_seeds,
        eval_seeds=tuple(args.eval_seeds),
        temperature=args.temperature,
        epochs=args.epochs,
        learning_rate=args.learning_rate,
        tasks=tuple(args.task),
    )
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    _stay_awake()
    asyncio.run(run(config))
    for entry in _progress(config):
        ev = entry.get("eval", {})
        print(
            f"{entry['round']}: eval fitness {ev.get('fitness', float('nan')):+.3f} "
            f"({ev.get('success', '?')}/{ev.get('attempts', '?')} success), "
            f"collected {entry.get('collect', {}).get('success', '-')} successes, "
            f"trained ({entry.get('train', {}).get('mode', '-')}) on "
            f"{entry.get('train', {}).get('rows', '-')} examples"
        )
    fitness, adapter = _best(config)
    print(f"best: {fitness:+.3f} -- {adapter or 'the base model'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
