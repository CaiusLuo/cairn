"""Exercise the actual required-check shell against GitHub dependency results."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml"


@pytest.mark.parametrize(
    ("scope", "docs", "checks", "acceptance", "success"),
    [
        ("success", "false", "success", "success", True),
        ("success", "true", "skipped", "skipped", True),
        ("failure", "true", "skipped", "skipped", False),
        ("cancelled", "false", "success", "success", False),
        ("skipped", "true", "skipped", "skipped", False),
        ("success", "", "skipped", "skipped", False),
        ("success", "true", "success", "skipped", False),
        *[
            ("success", "false", check, acceptance, False)
            for check, acceptance in [
                ("failure", "success"),
                ("cancelled", "success"),
                ("skipped", "success"),
                ("success", "failure"),
                ("success", "cancelled"),
                ("success", "skipped"),
                ("", "success"),
                ("success", ""),
            ]
        ],
    ],
)
def test_required_gate_rejects_missing_or_failed_work(
    scope: str, docs: str, checks: str, acceptance: str, success: bool
) -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    script = workflow["jobs"]["quality"]["steps"][0]["run"]
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
        env={
            **os.environ,
            "SCOPE": scope,
            "DOCS_ONLY": docs,
            "CHECKS": checks,
            "ACCEPTANCE": acceptance,
        },
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) is success, result.stdout + result.stderr
