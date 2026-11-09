"""Offline Gate B event safety checks; no live IDs, credentials or dispatch."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_gate_b_observation_without_financial_or_external_services():
    node = shutil.which("node")
    if node is None:
        pytest.fail("node is required for Gate B safety tests")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, "--test", str(root / "apps-script/receipt-gate-b/Code.test.cjs")],
        cwd=root, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
