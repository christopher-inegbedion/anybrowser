"""Reinforcement learning on recorded attempts: policy gradient, made cheap.

    python -m anybrowser_bench.rl_train RECORDINGS... --init ADAPTER --out DIR

Every recorded step is a reply the model actually sampled, so the update is
the plain policy gradient: make replies with positive advantage more likely
and replies with negative advantage less likely, in proportion.

**Reward.** A step earns its attempt's score from the judge (+1 success, 0 an
honest failure, -1 a false claim), plus ``label_weight`` times +1 or -1 for
whether its finish decision matched what the bench knew at that step: finish
when the goal was reached, with the right answer; keep going when it was not
(see anybrowser_bench.privileged).

**Advantage.** Reward minus the mean reward of the same task's steps in this
batch. Each task's advantages sum to zero, so no hand-set proportion decides
how much "finish" or "keep going" the model hears. Every earlier method had
such a proportion, and each one, in turn, swung the model to an extreme. Half
the trained steps are the most positive and half the most negative.

**Cost.** A naive pass runs the whole ~2,300-token prompt forward and backward
for a ~60-token reply: measured at 92-126s and 26.6 GB on an M4 (24s with
gradient checkpointing). Here the screenshot is encoded once, the prompt runs
forward once with no gradient into a KV cache, and only the reply is
differentiated against that cache: 5.5s and 4.6 GB, with reply log-probs within
0.005 per token of the full pass. The prompt's own representation gets no
gradient through the adapter; the reply's computation, which carries the
decision, does.

No KL term yet: one pass, a small learning rate, gradient clipping, and the
improve loop's selection (an adapter that scores worse is never adopted) bound
how far an update can go.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from random import Random
from typing import Any

from .dataset import _answer_right, _attempt_dirs, _parsed

__all__ = ["Sample", "build_samples", "train"]


@dataclass(frozen=True, slots=True)
class Sample:
    messages: tuple[dict[str, str], ...]
    image: Path
    reply: str
    advantage: float
    task: str
    reward: float


def _label_term(record: dict[str, Any], instance: dict[str, Any]) -> float:
    """+1 if the step's finish decision was right, -1 if wrong, 0 if unknown."""
    reached = (record.get("privileged") or {}).get("goal_reached")
    reply = _parsed(record["reply"])
    if reached is None or reply is None:
        return 0.0
    finished = reply.get("done") is True
    if reached and finished:
        return 1.0 if _answer_right(instance, str(reply.get("answer") or "")) else -1.0
    return 1.0 if reached == finished else -1.0


def build_samples(
    roots: Iterable[Path],
    *,
    label_weight: float = 0.5,
    max_samples: int = 150,
    min_advantage: float = 0.05,
) -> list[Sample]:
    """Every recorded step with an image, scored and centred per task."""
    raw: list[tuple[dict[str, Any], dict[str, Any], Path, float]] = []
    for attempt_dir in _attempt_dirs(roots):
        attempt = json.loads((attempt_dir / "attempt.json").read_text())
        instance = attempt.get("instance")
        if instance is None or attempt["outcome"] == "error":
            continue
        for step_path in sorted(attempt_dir.glob("step-[0-9][0-9].json")):
            record = json.loads(step_path.read_text())
            images = [attempt_dir / n for m in record["messages"] for n in m["images"]]
            if len(images) != 1 or _parsed(record["reply"]) is None:
                continue
            reward = float(attempt["score"]) + label_weight * _label_term(record, instance)
            raw.append((attempt, record, images[0], reward))

    by_task: dict[str, list[float]] = {}
    for attempt, _, _, reward in raw:
        by_task.setdefault(attempt["task_id"], []).append(reward)
    baseline = {task: sum(r) / len(r) for task, r in by_task.items()}

    samples = [
        Sample(
            messages=tuple({"role": m["role"], "content": m["text"]} for m in record["messages"]),
            image=image,
            reply=record["reply"],
            advantage=reward - baseline[attempt["task_id"]],
            task=attempt["task_id"],
            reward=reward,
        )
        for attempt, record, image, reward in raw
    ]
    samples = [s for s in samples if abs(s.advantage) >= min_advantage]
    positive = sorted((s for s in samples if s.advantage > 0), key=lambda s: -s.advantage)
    negative = sorted((s for s in samples if s.advantage < 0), key=lambda s: s.advantage)
    half = max_samples // 2
    chosen = positive[:half] + negative[:half]
    Random(0).shuffle(chosen)
    return chosen


def train(
    samples: Sequence[Sample],
    *,
    model_path: str,
    init: Path,
    out: Path,
    learning_rate: float = 1e-5,
    accumulate: int = 8,
    clip: float = 1.0,
    log: Any = print,
) -> dict[str, Any]:
    # mlx ships without complete type information; treat it as Any here.
    import mlx.core as mx
    import mlx.nn as _nn
    import mlx.optimizers as _optim
    import mlx.utils as _mlx_utils

    nn: Any = _nn
    optim: Any = _optim
    tree_flatten: Any = _mlx_utils.tree_flatten
    tree_map: Any = _mlx_utils.tree_map
    from mlx_vlm import utils as _vlm_utils
    from mlx_vlm.models.cache import make_prompt_cache
    from mlx_vlm.prompt_utils import apply_chat_template
    from mlx_vlm.trainer.utils import save_adapter

    load: Any = _vlm_utils.load
    prepare_inputs: Any = _vlm_utils.prepare_inputs

    model, processor = load(model_path, adapter_path=str(init))
    config = model.config.__dict__
    model.freeze()
    model.unfreeze(keys=["lora_a", "lora_b"], strict=False)
    model.train()
    optimizer = optim.Adam(learning_rate=learning_rate)
    vision_dtype = model.vision_tower.patch_embed.proj.weight.dtype
    image_token = config.get("image_token_id") or config.get("image_token_index")

    def encode(sample: Sample) -> tuple[Any, Any, Any, int] | None:
        messages = list(sample.messages)
        prompt = apply_chat_template(
            processor, config, messages, add_generation_prompt=True, num_images=1
        )
        full = apply_chat_template(
            processor,
            config,
            [*messages, {"role": "assistant", "content": sample.reply}],
            add_generation_prompt=False,
            num_images=1,
        )
        p = prepare_inputs(
            processor, images=[str(sample.image)], prompts=[prompt], image_token_index=image_token
        )
        f = prepare_inputs(
            processor, images=[str(sample.image)], prompts=[full], image_token_index=image_token
        )
        n = p["input_ids"].shape[1]
        if not (f["input_ids"][0, :n] == p["input_ids"][0]).all().item():
            return None  # the template did not keep the prompt as a prefix
        return f, p["input_ids"], f["input_ids"][:, n:], n

    def reply_logp(model: Any, cache: Any, prompt_ids: Any, reply_ids: Any, n: int) -> Any:
        m = reply_ids.shape[1]
        x = mx.concatenate([prompt_ids[:, n - 1 : n], reply_ids[:, : m - 1]], axis=1)
        logits = model.language_model(
            x, inputs_embeds=model.language_model.model.embed_tokens(x), cache=cache
        ).logits
        lp = nn.log_softmax(logits.astype(mx.float32), axis=-1)
        return mx.take_along_axis(lp, reply_ids[..., None], axis=-1).squeeze(-1)

    def loss_fn(
        model: Any, cache: Any, prompt_ids: Any, reply_ids: Any, n: int, advantage: float
    ) -> Any:
        return -advantage * reply_logp(model, cache, prompt_ids, reply_ids, n).mean()

    loss_and_grad = nn.value_and_grad(model, loss_fn)
    accumulated: Any = None
    steps, skipped, started = 0, 0, time.monotonic()
    for i, sample in enumerate(samples, start=1):
        encoded = encode(sample)
        if encoded is None:
            skipped += 1
            continue
        full, prompt_ids, reply_ids, n = encoded
        grid = full["image_grid_thw"]
        pixels = full["pixel_values"].astype(vision_dtype)
        # The screenshot once, the prompt once, neither differentiated.
        features = model.vision_tower(pixels, grid, output_hidden_states=False)
        cache = make_prompt_cache(model.language_model)
        model(
            prompt_ids[:, : n - 1],
            pixels,
            cache=cache,
            cached_image_features=features,
            image_grid_thw=grid,
        )
        mx.eval([c.state for c in cache])
        loss, grads = loss_and_grad(model, cache, prompt_ids, reply_ids, n, sample.advantage)
        accumulated = grads if accumulated is None else tree_map(mx.add, accumulated, grads)
        mx.eval(loss, accumulated)
        if i % accumulate == 0 or i == len(samples):
            mean = tree_map(lambda g: g / accumulate, accumulated)
            clipped, norm = optim.clip_grad_norm(mean, clip)
            optimizer.update(model, clipped)
            mx.eval(model.trainable_parameters(), optimizer.state)
            accumulated = None
            steps += 1
            log(
                f"sample {i}/{len(samples)}  update {steps}  grad-norm {norm.item():.3f}  "
                f"{(time.monotonic() - started) / i:.1f}s/sample"
            )

    out.mkdir(parents=True, exist_ok=True)
    save_adapter(model, out / "adapters.safetensors")
    if not (out / "adapter_config.json").exists():
        shutil.copyfile(init / "adapter_config.json", out / "adapter_config.json")
    trainable = sum(v.size for _, v in tree_flatten(model.trainable_parameters()))
    return {
        "samples": len(samples),
        "skipped": skipped,
        "updates": steps,
        "seconds": time.monotonic() - started,
        "trainable_parameters": trainable,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m anybrowser_bench.rl_train")
    parser.add_argument("recordings", type=Path, nargs="+")
    parser.add_argument("--init", type=Path, required=True, help="Adapter to start from.")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--model", default="mlx-community/Qwen2.5-VL-3B-Instruct-4bit")
    parser.add_argument("--max-samples", type=int, default=150)
    parser.add_argument("--label-weight", type=float, default=0.5)
    parser.add_argument("--learning-rate", type=float, default=1e-5)
    args = parser.parse_args(argv)
    samples = build_samples(
        args.recordings, label_weight=args.label_weight, max_samples=args.max_samples
    )
    print(
        f"{len(samples)} samples: {sum(s.advantage > 0 for s in samples)} positive, "
        f"{sum(s.advantage < 0 for s in samples)} negative",
        flush=True,
    )
    stats = train(
        samples,
        model_path=args.model,
        init=args.init,
        out=args.out,
        learning_rate=args.learning_rate,
        log=lambda m: print(m, flush=True),
    )
    (args.out / "rl_train.json").write_text(json.dumps(stats, indent=2))
    print(json.dumps(stats), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
