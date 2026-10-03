"""Turn recorded attempts into a fine-tuning set for mlx-vlm.

    python -m anybrowser_bench.dataset RECORDING_DIR [...] --out DATASET_DIR

One row per step, in the shape ``mlx_vlm.lora`` loads: the system prompt and
the user message the model was actually shown, its reply as the assistant
turn, and the screenshot. Nothing is reconstructed: every field comes verbatim
from :mod:`anybrowser_bench.record`.

What goes in is a choice, and these are the defaults:

- **Only attempts the judge scored a success.** Not the agent's own "done":
  the judge checked the seed or the server.
- **Only the steps that moved the attempt forward**, meaning the engine said the
  page changed, plus the final ``done``. A successful attempt can still contain
  a click repeated four times to no effect, and training on it teaches exactly
  the repetition that most often sank the baseline.
- **Unique steps only.** The same screenshot and reply recorded twice is one
  example, not two.

Train on seeds you will not evaluate on. The bench draws pages from the seed,
so held-out seeds are held-out pages.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["DatasetStats", "build_sft", "training_rows"]


@dataclass(slots=True)
class DatasetStats:
    attempts: int = 0
    successes: int = 0
    steps_seen: int = 0
    rows: int = 0
    dropped_no_effect: int = 0
    dropped_duplicate: int = 0

    def summary(self) -> str:
        return (
            f"{self.rows} rows from {self.successes}/{self.attempts} successful attempts "
            f"({self.steps_seen} steps; dropped {self.dropped_no_effect} with no effect, "
            f"{self.dropped_duplicate} duplicates)"
        )


def _attempt_dirs(roots: Iterable[Path]) -> Iterator[Path]:
    for root in roots:
        yield from sorted(p.parent for p in root.glob("*/attempt.json"))


def _keep(step_file: dict[str, Any]) -> bool:
    step = step_file.get("step")
    if step is None:
        return False  # the reply never became an action
    if step["tool"] == "done":
        return True
    return bool(step["ok"] and step["changed"])


def training_rows(
    roots: Iterable[Path], stats: DatasetStats | None = None
) -> Iterator[tuple[dict[str, Any], list[Path]]]:
    """(row, image source paths) for every step worth training on."""
    stats = stats if stats is not None else DatasetStats()
    seen: set[str] = set()
    for attempt_dir in _attempt_dirs(roots):
        attempt = json.loads((attempt_dir / "attempt.json").read_text())
        stats.attempts += 1
        if attempt["outcome"] != "success":
            continue
        stats.successes += 1
        for step_path in sorted(attempt_dir.glob("step-[0-9][0-9].json")):
            record = json.loads(step_path.read_text())
            stats.steps_seen += 1
            if not _keep(record):
                stats.dropped_no_effect += 1
                continue
            images = [attempt_dir / name for m in record["messages"] for name in m["images"]]
            digest = hashlib.sha256(record["reply"].encode())
            for image in images:
                digest.update(image.read_bytes())
            for message in record["messages"]:
                digest.update(message["text"].encode())
            key = digest.hexdigest()
            if key in seen:
                stats.dropped_duplicate += 1
                continue
            seen.add(key)
            messages = [{"role": m["role"], "content": m["text"]} for m in record["messages"]] + [
                {"role": "assistant", "content": record["reply"]}
            ]
            row = {
                "messages": messages,
                "meta": {
                    "task": attempt["task_id"],
                    "seed": attempt["seed"],
                    "step": step_path.stem,
                    "tool": record["step"]["tool"],
                },
            }
            stats.rows += 1
            yield row, images


def build_sft(roots: Iterable[Path], out: Path) -> DatasetStats:
    """Write ``out/train.jsonl`` and ``out/images/``. Image paths in the rows
    are absolute, so the dataset loads from any working directory."""
    stats = DatasetStats()
    images_dir = out / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    with (out / "train.jsonl").open("w") as handle:
        for index, (row, sources) in enumerate(training_rows(roots, stats)):
            copied = []
            for n, source in enumerate(sources):
                target = images_dir / f"{index:05d}-{n}{source.suffix}"
                shutil.copyfile(source, target)
                copied.append(str(target.resolve()))
            row["images"] = copied
            handle.write(json.dumps(row) + "\n")
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m anybrowser_bench.dataset")
    parser.add_argument("recordings", type=Path, nargs="+")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.out.exists() and any(args.out.iterdir()):
        parser.error(f"{args.out} is not empty; refusing to mix datasets")
    stats = build_sft(args.recordings, args.out)
    print(stats.summary())
    print(f"dataset: {args.out / 'train.jsonl'}")
    return 0 if stats.rows else 1


if __name__ == "__main__":
    sys.exit(main())
