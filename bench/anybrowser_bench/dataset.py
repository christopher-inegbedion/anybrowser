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
from dataclasses import dataclass, field
from pathlib import Path
from random import Random
from typing import Any

__all__ = [
    "DatasetStats",
    "PreferenceStats",
    "build_preferences",
    "build_privileged",
    "build_sft",
    "preference_pairs",
    "privileged_pairs",
    "training_rows",
]


@dataclass(slots=True)
class DatasetStats:
    attempts: int = 0
    successes: int = 0
    steps_seen: int = 0
    rows: int = 0
    dropped_no_effect: int = 0
    dropped_duplicate: int = 0
    dropped_balance: int = 0

    def summary(self) -> str:
        return (
            f"{self.rows} rows from {self.successes}/{self.attempts} successful attempts "
            f"({self.steps_seen} steps; dropped {self.dropped_no_effect} with no effect, "
            f"{self.dropped_duplicate} duplicates, {self.dropped_balance} to rebalance)"
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


def _rebalance(
    rows: list[tuple[dict[str, Any], list[Path]]],
    max_per_task: int | None,
    max_done_share: float | None,
) -> list[tuple[dict[str, Any], list[Path]]]:
    """Cap any one task, then cap the share of "done" rows.

    The first self-improvement run trained on 38 rows of which 32 were "done",
    18 of them from one task, and the model swung toward finishing early. A
    training set is a statement about what behaviour is typical; this keeps it
    from saying "finish" with most of its voice. Deterministic, so a rebuilt
    dataset is the same dataset.
    """
    rng = Random(0)
    by_task: dict[str, list[tuple[dict[str, Any], list[Path]]]] = {}
    for item in rows:
        by_task.setdefault(item[0]["meta"]["task"], []).append(item)
    kept: list[tuple[dict[str, Any], list[Path]]] = []
    for items in by_task.values():
        if max_per_task is not None and len(items) > max_per_task:
            items = rng.sample(items, max_per_task)
        kept += items
    if max_done_share is not None and 0 < max_done_share < 1:
        done = [r for r in kept if r[0]["meta"]["tool"] == "done"]
        acting = [r for r in kept if r[0]["meta"]["tool"] != "done"]
        allowed = int(max_done_share * len(acting) / (1 - max_done_share))
        if len(done) > allowed:
            # Round-robin over tasks, so the cut does not empty one task first.
            per_task: dict[str, list[tuple[dict[str, Any], list[Path]]]] = {}
            for r in done:
                per_task.setdefault(r[0]["meta"]["task"], []).append(r)
            queues = [rng.sample(q, len(q)) for q in per_task.values()]
            done = []
            while len(done) < allowed and any(queues):
                for q in queues:
                    if q and len(done) < allowed:
                        done.append(q.pop())
        kept = acting + done
    return kept


def _write_rows(rows: Iterable[tuple[dict[str, Any], list[Path]]], out: Path, name: str) -> int:
    images_dir = out / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    with (out / name).open("w") as handle:
        for index, (row, sources) in enumerate(rows):
            copied = []
            for n, source in enumerate(sources):
                target = images_dir / f"{index:05d}-{n}{source.suffix}"
                shutil.copyfile(source, target)
                copied.append(str(target.resolve()))
            row["images"] = copied
            handle.write(json.dumps(row) + "\n")
            count += 1
    return count


def build_sft(
    roots: Iterable[Path],
    out: Path,
    *,
    max_per_task: int | None = None,
    max_done_share: float | None = None,
) -> DatasetStats:
    """Write ``out/train.jsonl`` and ``out/images/``. Image paths in the rows
    are absolute, so the dataset loads from any working directory."""
    stats = DatasetStats()
    rows = list(training_rows(roots, stats))
    kept = _rebalance(rows, max_per_task, max_done_share)
    stats.dropped_balance = len(rows) - len(kept)
    stats.rows = _write_rows(kept, out, "train.jsonl")
    return stats


# --------------------------------------------------------------------------- #
# Preferences                                                                  #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class PreferenceStats:
    attempts: int = 0
    states: int = 0
    contested: int = 0
    pairs: int = 0
    dropped_to_balance: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"{self.pairs} pairs from {self.contested}/{self.states} states that more than "
            f"one attempt reached with different replies ({self.attempts} attempts); "
            f"{self.by_reason}"
        )


@dataclass(frozen=True, slots=True)
class _Reply:
    attempt: dict[str, Any]
    record: dict[str, Any]
    images: tuple[Path, ...]
    step: str

    @property
    def score(self) -> float:
        return float(self.attempt["score"])

    @property
    def acted_with_effect(self) -> bool | None:
        """True/False for an action that ran; None for done or no action."""
        step = self.record.get("step")
        if step is None or step["tool"] == "done":
            return None
        return bool(step["ok"] and step["changed"])

    @property
    def tool(self) -> str:
        step = self.record.get("step")
        return step["tool"] if step else "(unparsed)"


def _state_key(attempt_dir: Path, record: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    for message in record["messages"]:
        digest.update(message["role"].encode())
        digest.update(message["text"].encode())
        for name in message["images"]:
            digest.update((attempt_dir / name).read_bytes())
    return digest.hexdigest()


def _better(a: _Reply, b: _Reply) -> str:
    """Why ``a`` is preferred to ``b`` at the same state, or "" if it is not.

    By the outcome of the attempt each reply led into: a success beats an
    honest failure, which beats a false claim. That is the bench's own
    scoring, and it gives a signal even on tasks never solved: a premature
    "done" that ended in a false claim loses to an action from an attempt that
    kept going. Between attempts with the same outcome, an action the engine
    said changed the page beats one it said did nothing.
    """
    if a.score > b.score:
        return {
            (1.0, 0.0): "success>failed",
            (1.0, -1.0): "success>false_claim",
            (0.0, -1.0): "failed>false_claim",
        }.get((a.score, b.score), "higher score")
    if a.score == b.score and a.acted_with_effect is True and b.acted_with_effect is False:
        return "effect>no_effect"
    return ""


def preference_pairs(
    roots: Iterable[Path],
    stats: PreferenceStats | None = None,
    *,
    max_pairs_per_state: int = 2,
    max_pairs_per_task: int | None = None,
) -> Iterator[tuple[dict[str, Any], list[Path]]]:
    """(row, images) for every chosen/rejected pair at an identical state.

    "Identical" is strict: the same system prompt, the same user message --
    which carries the history -- and the same screenshot bytes. Repeated
    attempts at one seed share their first state; they share later ones only
    while their actions agree. A pair is only built where the model, shown
    exactly the same thing, answered differently and the outcomes say which
    answer was better: the states where it is inconsistent, which are the
    states worth learning from.
    """
    stats = stats if stats is not None else PreferenceStats()
    states: dict[str, list[_Reply]] = {}
    for attempt_dir in _attempt_dirs(roots):
        attempt = json.loads((attempt_dir / "attempt.json").read_text())
        if attempt["outcome"] == "error":
            continue  # says nothing about the model
        stats.attempts += 1
        for step_path in sorted(attempt_dir.glob("step-[0-9][0-9].json")):
            record = json.loads(step_path.read_text())
            images = tuple(attempt_dir / n for m in record["messages"] for n in m["images"])
            states.setdefault(_state_key(attempt_dir, record), []).append(
                _Reply(attempt, record, images, step_path.stem)
            )
    stats.states = len(states)
    per_task: dict[str, int] = {}
    seen: set[tuple[str, str]] = set()
    for replies in states.values():
        if len({r.record["reply"] for r in replies}) < 2:
            continue
        stats.contested += 1
        ranked = sorted(
            replies,
            key=lambda r: (r.score, r.acted_with_effect is True, r.acted_with_effect is not False),
            reverse=True,
        )
        best = ranked[0]
        made = 0
        for worse in reversed(ranked[1:]):
            if made >= max_pairs_per_state:
                break
            reason = _better(best, worse)
            pair = (best.record["reply"], worse.record["reply"])
            if not reason or pair[0] == pair[1] or pair in seen:
                continue
            task = best.attempt["task_id"]
            if max_pairs_per_task is not None and per_task.get(task, 0) >= max_pairs_per_task:
                break
            seen.add(pair)
            per_task[task] = per_task.get(task, 0) + 1
            made += 1
            prompt = [{"role": m["role"], "content": m["text"]} for m in best.record["messages"]]
            row = {
                "chosen": [*prompt, {"role": "assistant", "content": pair[0]}],
                "rejected": [*prompt, {"role": "assistant", "content": pair[1]}],
                "meta": {
                    "task": task,
                    "seed": best.attempt["seed"],
                    "step": best.step,
                    "chosen_tool": best.tool,
                    "rejected_tool": worse.tool,
                    "why": reason,
                },
            }
            stats.pairs += 1
            stats.by_reason[reason] = stats.by_reason.get(reason, 0) + 1
            yield row, list(best.images)


def _balance_finishing(
    rows: list[tuple[dict[str, Any], list[Path]]], max_ratio: float
) -> list[tuple[dict[str, Any], list[Path]]]:
    """Cap pairs whose chosen reply keeps acting at ``max_ratio`` x those whose
    chosen reply finishes.

    ORPO is not only a comparison: its loss includes plain likelihood on every
    chosen reply, so the chosen side is also an SFT set. In the first ORPO run
    41 of 49 chosen replies said ``"done": false``. The model learned "keep
    going" as a prior, and by round 4 it would not finish even canvas-read --
    "the high score is not visible", with the score on screen -- which it had
    passed 5/5 since round 0. The same imbalance as the success-only run, the
    other way round. Deterministic, and round-robin over tasks so the cut does
    not empty one task first.
    """
    finishing = [r for r in rows if r[0]["meta"]["chosen_tool"] == "done"]
    acting = [r for r in rows if r[0]["meta"]["chosen_tool"] != "done"]
    allowed = int(max_ratio * len(finishing))
    if len(acting) <= allowed:
        return rows
    by_task: dict[str, list[tuple[dict[str, Any], list[Path]]]] = {}
    for r in acting:
        by_task.setdefault(r[0]["meta"]["task"], []).append(r)
    rng = Random(0)
    queues = [rng.sample(q, len(q)) for q in by_task.values()]
    kept: list[tuple[dict[str, Any], list[Path]]] = []
    while len(kept) < allowed and any(queues):
        for q in queues:
            if q and len(kept) < allowed:
                kept.append(q.pop())
    return finishing + kept


def build_preferences(
    roots: Iterable[Path],
    out: Path,
    *,
    max_pairs_per_task: int | None = None,
    max_acting_per_finishing: float | None = 2.0,
) -> PreferenceStats:
    """Write ``out/train.jsonl`` in the shape ``mlx_vlm.lora --train-mode orpo``
    loads: ``chosen`` and ``rejected`` conversations sharing one prompt and
    screenshot. ``max_acting_per_finishing`` balances the chosen side; see
    :func:`_balance_finishing`."""
    stats = PreferenceStats()
    rows = list(preference_pairs(roots, stats, max_pairs_per_task=max_pairs_per_task))
    if max_acting_per_finishing is not None:
        balanced = _balance_finishing(rows, max_acting_per_finishing)
        stats.dropped_to_balance = len(rows) - len(balanced)
        stats.pairs = len(balanced)
        rows = balanced
    _write_rows(rows, out, "train.jsonl")
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m anybrowser_bench.dataset")
    parser.add_argument("recordings", type=Path, nargs="+")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--preferences",
        action="store_true",
        help="Chosen/rejected pairs at identical states, for ORPO, instead of SFT rows.",
    )
    parser.add_argument("--max-per-task", type=int, default=None)
    parser.add_argument("--max-done-share", type=float, default=None)
    args = parser.parse_args(argv)
    if args.out.exists() and any(args.out.iterdir()):
        parser.error(f"{args.out} is not empty; refusing to mix datasets")
    if args.preferences:
        pstats = build_preferences(args.recordings, args.out, max_pairs_per_task=args.max_per_task)
        print(pstats.summary())
        count = pstats.pairs
    else:
        stats = build_sft(
            args.recordings,
            args.out,
            max_per_task=args.max_per_task,
            max_done_share=args.max_done_share,
        )
        print(stats.summary())
        count = stats.rows
    print(f"dataset: {args.out / 'train.jsonl'}")
    return 0 if count else 1


if __name__ == "__main__":
    sys.exit(main())


# --------------------------------------------------------------------------- #
# Privileged labels and hindsight                                              #
# --------------------------------------------------------------------------- #
#
# Outcome pairs need two attempts to reach the identical screen and then
# diverge: 30-50 pairs from 90 attempts. With what the bench knew at each step
# (anybrowser_bench.privileged) every recorded screen carries the right answer
# to "should I finish now?", so every wrong finish decision becomes a pair --
# from failed attempts too, and on tasks never solved.

#: Arguments a vision tool cannot run without; a counterfactual action missing
#: them is not offered as the better reply.
_REQUIRED = {
    "click": ("x", "y"),
    "type_text": ("text",),
    "press_key": ("key",),
    "navigate": ("url",),
    "scroll": (),
    "wait": (),
}


def _reply(
    narrative: str,
    *,
    done: bool,
    answer: str = "",
    tool: str = "wait",
    arguments: dict[str, Any] | None = None,
) -> str:
    # Key order as the constrained schema has it (llm_planner.decision_schema).
    return json.dumps(
        {
            "narrative": narrative,
            "done": done,
            "answer": answer,
            "tool": tool,
            "arguments": arguments or {},
        }
    )


def _finish(instance: dict[str, Any]) -> str:
    """The right final reply, written from what the judge accepts."""
    if instance["kind"] == "impossible":
        return _reply("The item is not listed on the page.", done=True, answer="NOT FOUND")
    if instance["kind"] == "state":
        return _reply("The page shows the goal has been completed.", done=True, answer="Done.")
    expected = instance["expected"]
    return _reply(
        f"The page shows {expected}, which is what the goal asks for.", done=True, answer=expected
    )


def _parsed(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _answer_right(instance: dict[str, Any], answer: str) -> bool:
    from .task import Instance, Kind, judge

    kind = Kind(instance["kind"])
    probe = Instance(goal="", pages={}, kind=kind, expected=instance["expected"])
    if kind is Kind.STATE:
        return True  # the server decides those; the oracle already said so
    return judge(probe, finished=True, answer=answer, submissions=[]).outcome.value == "success"


def _with_goal(messages: list[dict[str, Any]], goal: str) -> list[dict[str, Any]]:
    """The same prompt, asking for ``goal`` instead."""
    out = []
    for m in messages:
        text = m["content"]
        if m["role"] == "user" and text.startswith("GOAL: "):
            text = f"GOAL: {goal}" + text[text.index("\n") :] if "\n" in text else f"GOAL: {goal}"
        out.append({"role": m["role"], "content": text})
    return out


def _hindsight_goal(step: dict[str, Any], target: dict[str, Any] | None) -> str:
    """The goal a step that changed the page in fact achieved, or ""."""
    if not (step["ok"] and step["changed"]) or not target or target.get("canvas"):
        return ""
    name = str(target.get("text") or "").strip()
    if step["tool"] == "click" and name:
        return f'Click "{name}".'
    if step["tool"] == "type_text":
        typed = str(step["arguments"].get("text") or "")
        if not typed:
            return ""
        return f'Type "{typed}" into the "{name}" field.' if name else f'Type "{typed}".'
    return ""


def privileged_pairs(
    roots: Iterable[Path], stats: PreferenceStats | None = None, *, hindsight: bool = False
) -> Iterator[tuple[dict[str, Any], list[Path]]]:
    """Label pairs and hindsight pairs from recordings that carry privileged
    facts. Recordings without them (older runs) contribute nothing here."""
    stats = stats if stats is not None else PreferenceStats()

    def pair(
        prompt: list[dict[str, Any]],
        chosen: str,
        rejected: str,
        images: list[Path],
        meta: dict[str, Any],
    ) -> tuple[dict[str, Any], list[Path]]:
        stats.pairs += 1
        stats.by_reason[meta["why"]] = stats.by_reason.get(meta["why"], 0) + 1
        return (
            {
                "chosen": [*prompt, {"role": "assistant", "content": chosen}],
                "rejected": [*prompt, {"role": "assistant", "content": rejected}],
                "meta": meta,
            },
            images,
        )

    for attempt_dir in _attempt_dirs(roots):
        attempt = json.loads((attempt_dir / "attempt.json").read_text())
        instance = attempt.get("instance")
        if instance is None or attempt["outcome"] == "error":
            continue
        stats.attempts += 1
        steps = []
        for step_path in sorted(attempt_dir.glob("step-[0-9][0-9].json")):
            record = json.loads(step_path.read_text())
            images = [attempt_dir / n for m in record["messages"] for n in m["images"]]
            prompt = [{"role": m["role"], "content": m["text"]} for m in record["messages"]]
            steps.append((step_path.stem, record, images, prompt))

        for name, record, images, prompt in steps:
            facts = record.get("privileged") or {}
            reached = facts.get("goal_reached")
            reply = _parsed(record["reply"])
            if reply is None or reached is None:
                continue
            stats.states += 1
            finished = reply.get("done") is True
            base = {"task": attempt["task_id"], "seed": attempt["seed"], "step": name}
            if reached and not finished:
                yield pair(
                    prompt,
                    _finish(instance),
                    record["reply"],
                    images,
                    {
                        **base,
                        "chosen_tool": "done",
                        "rejected_tool": str(reply.get("tool")),
                        "why": "label:finish_now",
                    },
                )
            elif reached and finished and not _answer_right(instance, str(reply.get("answer"))):
                yield pair(
                    prompt,
                    _finish(instance),
                    record["reply"],
                    images,
                    {
                        **base,
                        "chosen_tool": "done",
                        "rejected_tool": "done",
                        "why": "label:right_answer",
                    },
                )
            elif not reached and finished:
                tool = str(reply.get("tool"))
                arguments = reply.get("arguments") or {}
                if tool in _REQUIRED and all(k in arguments for k in _REQUIRED[tool]):
                    # Its own intended action, without the premature finish.
                    acting = _reply(
                        str(reply.get("narrative") or ""),
                        done=False,
                        tool=tool,
                        arguments=dict(arguments),
                    )
                    yield pair(
                        prompt,
                        acting,
                        record["reply"],
                        images,
                        {
                            **base,
                            "chosen_tool": tool,
                            "rejected_tool": "done",
                            "why": "label:not_yet",
                        },
                    )

        if not hindsight or attempt["outcome"] == "success":
            continue  # hindsight is for attempts that missed their own goal
        if instance["kind"] != "state":
            # Only where "act, then finish" is the shape of the real task. On an
            # answer task the finish has to read something, and pairs teaching
            # 'Click "Search". -> act -> "Done."' taught exactly that: the next
            # model clicked Search on delayed-search and finished with "Done.",
            # 5 of 5, before any result could load.
            continue
        for k, (name, record, images, prompt) in enumerate(steps):
            step = record.get("step")
            if not step or step["tool"] == "done":
                continue
            goal = _hindsight_goal(step, (record.get("privileged") or {}).get("target"))
            if not goal:
                continue
            base = {
                "task": attempt["task_id"],
                "seed": attempt["seed"],
                "step": name,
                "hindsight_goal": goal,
            }
            reply = _parsed(record["reply"]) or {}
            acting = _reply(
                str(reply.get("narrative") or ""),
                done=False,
                tool=step["tool"],
                arguments=dict(step["arguments"]),
            )
            premature = _reply("The goal is complete.", done=True, answer="Done.")
            yield pair(
                _with_goal(prompt, goal),
                acting,
                premature,
                images,
                {
                    **base,
                    "chosen_tool": step["tool"],
                    "rejected_tool": "done",
                    "why": "hindsight:act",
                },
            )
            if k + 1 < len(steps):
                after_name, after, after_images, after_prompt = steps[k + 1]
                if (_parsed(after["reply"]) or {}).get("done") is not True:
                    finish = _reply("That is done: the page shows it.", done=True, answer="Done.")
                    yield pair(
                        _with_goal(after_prompt, goal),
                        finish,
                        after["reply"],
                        after_images,
                        {
                            **base,
                            "step": after_name,
                            "chosen_tool": "done",
                            "rejected_tool": str((_parsed(after["reply"]) or {}).get("tool")),
                            "why": "hindsight:finish",
                        },
                    )


def build_privileged(
    roots: Iterable[Path],
    out: Path,
    *,
    max_pairs: int = 60,
    max_acting_per_finishing: float | None = 2.0,
    hindsight: bool = False,
) -> PreferenceStats:
    """Label, hindsight and outcome pairs together, balanced, at most
    ``max_pairs`` (an ORPO step is ~48s on the reference machine).

    Selection is round-robin over (reason, task), so neither the commonest
    mistake nor the commonest task fills the set.
    """
    roots = list(roots)
    stats = PreferenceStats()
    # Hindsight is off by default. With it, both rounds that used it put new
    # false claims on exactly the tasks it covered: answer tasks in one round
    # (delayed-search, async-status), state tasks in the next (form-fill,
    # cookie-wall). 'Click "Full name". -> act -> finish' is made on a screen
    # indistinguishable from the real form-fill task, and a 3B model does not
    # condition on the goal text firmly enough to tell a sub-goal from the goal.
    rows = list(privileged_pairs(roots, stats, hindsight=hindsight))
    outcome_stats = PreferenceStats()
    rows += list(preference_pairs(roots, outcome_stats))
    for reason, n in outcome_stats.by_reason.items():
        stats.by_reason[reason] = stats.by_reason.get(reason, 0) + n
    seen: set[tuple[str, str, str]] = set()
    unique = []
    for row, images in rows:
        key = (
            json.dumps(row["chosen"][:-1]),
            row["chosen"][-1]["content"],
            row["rejected"][-1]["content"],
        )
        if key not in seen:
            seen.add(key)
            unique.append((row, images))
    balanced = (
        unique
        if max_acting_per_finishing is None
        else _balance_finishing(unique, max_acting_per_finishing)
    )
    rng = Random(0)
    buckets: dict[tuple[str, str], list[tuple[dict[str, Any], list[Path]]]] = {}
    for item in balanced:
        meta = item[0]["meta"]
        buckets.setdefault((meta["why"], meta["task"]), []).append(item)
    queues = [rng.sample(q, len(q)) for q in buckets.values()]
    chosen: list[tuple[dict[str, Any], list[Path]]] = []
    while len(chosen) < max_pairs and any(queues):
        for q in queues:
            if q and len(chosen) < max_pairs:
                chosen.append(q.pop())
    stats.dropped_to_balance = len(rows) - len(chosen)
    stats.pairs = _write_rows(chosen, out, "train.jsonl")
    return stats
