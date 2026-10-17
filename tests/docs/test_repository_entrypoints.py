"""Repository scripts remain executable after asset layout changes."""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "entrypoint",
    [
        ("evaluation/runners/run_coding_smoke.py",),
        ("evaluation/runners/run_repository_intelligence.py",),
        ("-m", "evaluation.benchmarks.session_checkpoints"),
        ("scripts/github/run_issue_task.py",),
        ("scripts/pilot/preflight.py",),
    ],
)
def test_repository_entrypoint_help_is_available(entrypoint: tuple[str, ...]) -> None:
    result = subprocess.run(
        [sys.executable, *entrypoint, "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
