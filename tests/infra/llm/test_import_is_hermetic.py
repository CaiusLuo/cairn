"""Importing Cairn must not reach the network.

LiteLLM downloads its model cost map at import time, which made `--help` on the
evaluation runners, and the tests that start them, depend on network latency.
Both the Alibaba Java manual and Software Engineering at Google require unit
tests to avoid external environments; a hermetic import is the way to get there.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_importing_the_provider_package_selects_the_bundled_cost_map() -> None:
    """The package settles LiteLLM's cost-map source before the SDK loads."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if key != "LITELLM_LOCAL_MODEL_COST_MAP"
    }

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, cairn.infra.llm; print(os.environ['LITELLM_LOCAL_MODEL_COST_MAP'])",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


def test_an_explicit_choice_still_wins() -> None:
    """A caller that wants the refresh keeps it; the default is only a default."""
    environment = {**os.environ, "LITELLM_LOCAL_MODEL_COST_MAP": "False"}

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, cairn.infra.llm; print(os.environ['LITELLM_LOCAL_MODEL_COST_MAP'])",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"
