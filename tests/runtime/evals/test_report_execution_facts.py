"""The report records the real execution fact, and old reports stay readable.

The shell is local-first: an eval run refuses it unless it opts in. A report must
state whether the shell was granted, and must never turn "the shell was not
granted" into a claim that the network was isolated.
"""

import json
from pathlib import Path
from typing import Literal

import pytest

from cairn.core.budget import RunBudget
from cairn.core.context import ContextBudget
from cairn.runtime.evals.intelligence.dataset import dataset_digest
from cairn.runtime.evals.intelligence.experiment import run_experiment
from cairn.runtime.evals.intelligence.models import ExperimentConfig, ExperimentReport
from cairn.runtime.evals.intelligence.reporting import comparison, findings
from cairn.runtime.evals.models import EvalSuiteConfig
from tests.runtime.evals.test_intelligence_adapters import simple_spec
from tests.support.runtime import SequenceLLM


def _config(
    shell: Literal["granted", "refused", "unknown"] = "refused",
) -> ExperimentConfig:
    spec = simple_spec()
    return ExperimentConfig(
        dataset_sha256=dataset_digest((spec,)),
        execution_kind="offline",
        model_identifier="offline-model",
        case_names=(spec.item.case.name,),
        settings=EvalSuiteConfig(
            run_budget=RunBudget(max_steps=3),
            context_budget=ContextBudget(),
            run_timeout_seconds=10,
            check_timeout_seconds=2,
        ),
        shell_execution=shell,
    )


def test_findings_state_the_shell_fact_not_a_sandbox_guess() -> None:
    text = findings(
        ExperimentReport(config=_config()),
        comparison(ExperimentReport(config=_config())),
    )
    assert "Shell execution: refused" in text
    assert "no OS sandbox" in text
    # The old, false claim must not reappear.
    assert "bubblewrap" not in text
    assert "sandbox-exec" not in text
    assert "baseline-deny-no-approval" not in text


def test_a_claim_of_a_granted_shell_is_rejected_by_the_refusing_runner(
    tmp_path: Path,
) -> None:
    spec = simple_spec()
    config = _config(shell="granted")
    config.dataset_sha256 = dataset_digest((spec,))
    with pytest.raises(ValueError, match="refuses the shell"):
        pytest.importorskip("asyncio").run(
            run_experiment(
                (spec,),
                config,
                lambda: SequenceLLM(
                    [
                        __import__(
                            "cairn.core.models", fromlist=["LLMResponse"]
                        ).LLMResponse(content="x")
                    ]
                ),
                directory=tmp_path,
            )
        )


def test_an_old_report_with_sandbox_and_network_policy_still_reads() -> None:
    """A V1 file keeps its fields; the unknown shell fact is explicit."""
    raw = {
        "schema_version": 1,
        "config": {
            "dataset_version": "repo-intelligence-v1",
            "dataset_sha256": "abc",
            "execution_kind": "real",
            "model_identifier": "m",
            "case_names": ["c"],
            "settings": {
                "run_budget": {"max_steps": 1},
                "context_budget": {"max_tokens": 8, "response_tokens": 1},
                "run_timeout_seconds": 1,
                "check_timeout_seconds": 1,
            },
            "sandbox": "macos-sandbox-exec",
            "network_policy": "baseline-deny-no-approval",
        },
        "state": "completed",
        "runs": [],
    }
    report = ExperimentReport.model_validate(raw)
    # The retired fields are ignored, and the fact defaults to unknown rather
    # than being invented from the old sandbox string.
    assert report.config.shell_execution == "unknown"


def test_a_new_report_persists_the_shell_fact(tmp_path: Path) -> None:
    from cairn.runtime.evals.intelligence.reporting import persist

    report = ExperimentReport(config=_config(shell="refused"))
    persist(report, tmp_path)
    saved = json.loads((tmp_path / "experiment.json").read_text())
    assert saved["config"]["shell_execution"] == "refused"
    assert "sandbox" not in saved["config"]
    assert "network_policy" not in saved["config"]
