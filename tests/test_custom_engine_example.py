"""The reference engine should explain itself when executed."""

import subprocess
import sys
from pathlib import Path


def test_custom_engine_example_has_a_useful_trace() -> None:
    example = Path(__file__).resolve().parents[1] / "examples" / "custom_engine.py"
    result = subprocess.run(
        [sys.executable, str(example)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "driving toy 0 via toy" in result.stdout
    assert "https://example.com: 0 interactive elements" in result.stdout
    assert "click: ok=True changed=False" in result.stdout
    assert "No real browser was opened" in result.stdout
    assert result.stderr == ""
