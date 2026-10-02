"""The reference skeleton explains itself when run directly."""

import subprocess
import sys
from pathlib import Path


def test_custom_engine_example_explains_usage() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "examples" / "custom_engine.py")],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "reference skeleton" in result.stdout
    assert "pytest --pyargs anybrowser_conformance --engine toy" in result.stdout
    assert "changed=False" in result.stdout
