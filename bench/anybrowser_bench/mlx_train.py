"""``python -m anybrowser_bench.mlx_train ARGS``: ``mlx_vlm.lora``, with one fix.

mlx-vlm's ORPO path cannot train Qwen2.5-VL. Its preference dataset keeps
three of the processor's outputs -- ``input_ids``, ``attention_mask`` and
``pixel_values`` -- and drops the rest, including ``image_grid_thw``, the
image layout Qwen2.5-VL's vision tower needs to place its patches. The first
step then fails in ``rot_pos_emb`` with ``'NoneType' object has no attribute
'tolist'``. The SFT dataset passes everything through, which is why SFT works.

The collate step already forwards any extra ``chosen_*`` / ``rejected_*``
field, so the first fix is only to keep them. The second is that ORPO's step
is compiled and this model cannot be: see :func:`_run_orpo_step_uncompiled`.
Both are applied here, at runtime, rather than by editing the installed
package. Arguments are passed to ``mlx_vlm.lora`` unchanged. Measured against
mlx-vlm 0.7.4.
"""

from __future__ import annotations

import runpy
import sys
from typing import Any

_KEPT = ("input_ids", "attention_mask", "pixel_values")


def _keep_every_processor_output() -> None:
    import mlx_vlm.trainer.datasets as datasets

    module: Any = datasets
    original_process: Any = datasets.PreferenceVisionDataset.process
    original_to_mlx = module.to_mlx

    def process(self: Any, item: Any) -> Any:
        captured: list[dict[str, Any]] = []

        def capture(inputs: Any) -> Any:
            converted = original_to_mlx(inputs)
            captured.append(converted)
            return converted

        module.to_mlx = capture
        try:
            result = original_process(self, item)
        finally:
            module.to_mlx = original_to_mlx
        # One conversion per side, in the order process() visits them.
        for side, converted in zip(("chosen", "rejected"), captured, strict=False):
            for key, value in converted.items():
                if key in _KEPT or value is None:
                    continue
                result.setdefault(f"{side}_{key}", value)
        return result

    datasets.PreferenceVisionDataset.process = process  # type: ignore[method-assign]


class _MxWithoutCompile:
    """mlx.core, except that ``compile`` returns the function unchanged."""

    def __getattr__(self, name: str) -> Any:
        import mlx.core

        return getattr(mlx.core, name)

    @staticmethod
    def compile(fun: Any = None, **_: Any) -> Any:
        return fun if fun is not None else (lambda f: f)


def _run_orpo_step_uncompiled() -> None:
    """The second fault: ORPO's training step is wrapped in ``mx.compile``,
    and Qwen2.5-VL's vision tower reads ``image_grid_thw`` as Python integers
    (``.tolist()`` in ``rot_pos_emb``) -- a data-dependent read a compiled
    function cannot make, whatever type the value arrives as. The SFT step is
    not compiled, which is why SFT trains this model. Running ORPO's step the
    same way costs speed, not correctness."""
    import mlx_vlm.trainer.orpo_trainer as orpo

    module: Any = orpo
    module.mx = _MxWithoutCompile()


def main(argv: list[str] | None = None) -> None:
    _keep_every_processor_output()
    _run_orpo_step_uncompiled()
    sys.argv = ["mlx_vlm.lora", *(sys.argv[1:] if argv is None else argv)]
    runpy.run_module("mlx_vlm.lora", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
