import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from cairn.core.budget import RunBudget
from cairn.core.context import ContextBudget
from cairn.core.models import LLMResponse, LLMUsage, Message, ToolCall
from cairn.evals.intelligence.dataset import dataset_digest
from cairn.evals.intelligence.experiment import execution_plan, run_experiment
from cairn.evals.intelligence.models import (
    Arm,
    ExperimentConfig,
    ExperimentReport,
    ExperimentRun,
    IntelligenceMetrics,
)
from cairn.evals.intelligence.reporting import comparison, persist
from cairn.evals.models import EvalResult, EvalStatus, EvalSuiteConfig
from tests.evals.test_intelligence_adapters import simple_spec
from tests.support.runtime import SequenceLLM


def config(*, real: bool = False) -> ExperimentConfig:
    spec = simple_spec()
    return ExperimentConfig(
        dataset_sha256=dataset_digest((spec,)),
        execution_kind="real" if real else "offline",
        model_identifier="offline-model",
        case_names=(spec.item.case.name,),
        settings=EvalSuiteConfig(
            run_budget=RunBudget(max_steps=3),
            context_budget=ContextBudget(),
            run_timeout_seconds=10,
            check_timeout_seconds=2,
        ),
        sandbox="test",
    )


def test_schedule_is_bounded_balanced_and_reproducible() -> None:
    specs = (simple_spec(),)
    plan = execution_plan(specs, 10, 3)
    assert plan == execution_plan(specs, 10, 3)
    assert plan != execution_plan(specs, 11, 3)
    assert len(plan) == 12
    for rep in (1, 2, 3):
        assert {arm for _, arm, repetition in plan if repetition == rep} == set(Arm)
    with pytest.raises(ValueError):
        execution_plan(specs, 1, 4)
    with pytest.raises(ValueError):
        execution_plan(specs * 2, 1, 1)


def test_actual_metrics_and_artifacts(tmp_path: Path) -> None:
    # Two reads of the same known relevant file are explicitly observable;
    # Bash intent outside the documented classifier is not inferred.
    def factory() -> SequenceLLM:
        return SequenceLLM(
            [
                LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="read1",
                            name="read_file",
                            arguments={"path": "parser.py"},
                        ),
                        ToolCall(
                            id="read2",
                            name="read_file",
                            arguments={"path": "parser.py"},
                        ),
                    ],
                    usage=LLMUsage(input_tokens=10, output_tokens=3),
                ),
                LLMResponse(
                    content="done", usage=LLMUsage(input_tokens=20, output_tokens=5)
                ),
            ]
        )

    directory = tmp_path / "reports"
    report = asyncio.run(
        run_experiment(
            (simple_spec(),), config(), factory, directory=directory, temp_root=tmp_path
        )
    )
    assert report.state == "completed" and report.real_provider_status is None
    assert len(report.runs) == 4
    for run in report.runs:
        m = run.metrics
        assert m.tool_calls == m.read_file_calls == 2
        assert m.repeated_read_file_calls == 1
        assert len(m.direct_discovery_evidence) == 1
        assert m.provider_input_tokens == 30 and m.provider_output_tokens == 8
        assert (
            len(m.request_token_counts)
            == m.provider_requests
            == m.provider_responses
            == 2
        )
        assert m.request_tokens_total == sum(
            count.tokens for count in m.request_token_counts
        )
        assert (
            json.loads((directory / "runs" / f"{run.run_id}.json").read_text())[
                "metrics"
            ]["read_file_calls"]
            == 2
        )
    summary = comparison(report)
    assert summary["arms"]["A"]["task_success_rate"] == 1
    assert len(summary["per_case_against_baseline"]) == 3
    assert "offline" in (directory / "findings.md").read_text()
    assert not list(tmp_path.glob("cairn-eval-*"))


def test_missing_data_and_failed_check_aggregation(tmp_path: Path) -> None:
    report = ExperimentReport(config=config())
    for arm, input_tokens, status in (
        (Arm.BASELINE, None, EvalStatus.FAIL),
        (Arm.GRAPH, 12, EvalStatus.PASS),
    ):
        report.runs.append(
            ExperimentRun(
                run_id=arm.value,
                case_name="parser",
                arm=arm,
                repetition=1,
                order_index=0,
                result=EvalResult(case_name="parser", status=status),
                metrics=IntelligenceMetrics(provider_input_tokens=input_tokens),
            )
        )
    summary = comparison(report)
    assert summary["arms"]["A"]["measurements"]["provider_input_tokens"] == {
        "available": 0,
        "total": None,
        "mean": None,
    }
    assert summary["arms"]["C"]["task_success_rate"] is None
    row = summary["per_case_against_baseline"][0]
    assert (
        row["pass_delta"] == 1 and row["differences"]["provider_input_tokens"] is None
    )
    persist(report, tmp_path)
    assert (
        json.loads((tmp_path / "comparison.json").read_text())["arms"]["D"][
            "relevant_memory_precision"
        ]
        is None
    )


def test_provider_unavailable_stops_real_pilot_and_sanitizes(tmp_path: Path) -> None:
    class Fails(SequenceLLM):
        async def generate(
            self, messages: list[Message], tools: list[dict[str, Any]] | None = None
        ) -> LLMResponse:
            raise RuntimeError("credential=secret request=private")

    report = asyncio.run(
        run_experiment(
            (simple_spec(),),
            config(real=True),
            lambda: Fails([]),
            directory=tmp_path / "reports",
            temp_root=tmp_path,
        )
    )
    assert (
        report.state == "real_eval_blocked" and report.real_provider_status == "BLOCKED"
    )
    assert len(report.runs) == 1 and report.runs[0].metrics.provider_failures == 1
    assert report.runs[0].metrics.provider_responses == 0
    assert report.runs[0].result.status is EvalStatus.ERROR
    assert report.runs[0].metrics.provider_input_tokens is None
    for file in (tmp_path / "reports").rglob("*"):
        if file.is_file():
            text = file.read_text()
            assert "credential=" not in text and "private" not in text


def test_cancel_persists_active_metrics_and_cleans_up(tmp_path: Path) -> None:
    entered = asyncio.Event()

    class Waiting(SequenceLLM):
        async def generate(
            self, messages: list[Message], tools: list[dict[str, Any]] | None = None
        ) -> LLMResponse:
            entered.set()
            await asyncio.Event().wait()
            return LLMResponse(content="unreachable")

    async def cancel() -> None:
        task = asyncio.create_task(
            run_experiment(
                (simple_spec(),),
                config(),
                lambda: Waiting([]),
                directory=tmp_path / "reports",
                temp_root=tmp_path,
            )
        )
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel())
    report = ExperimentReport.model_validate_json(
        (tmp_path / "reports" / "experiment.json").read_text()
    )
    assert report.state == "interrupted" and report.active_metrics is not None
    assert report.active_metrics.provider_requests == 1 and report.active_run_id
    assert not list(tmp_path.glob("cairn-eval-*"))


def test_configuration_rejects_changed_dataset_before_calls(tmp_path: Path) -> None:
    settings = config().model_copy(update={"dataset_sha256": "incorrect"})
    with pytest.raises(ValueError, match="frozen dataset"):
        asyncio.run(
            run_experiment(
                (simple_spec(),), settings, lambda: SequenceLLM([]), directory=tmp_path
            )
        )
    assert list(tmp_path.iterdir()) == []


def test_frozen_dataset_all_arms_noop_outcomes_and_memory_isolation(
    tmp_path: Path,
) -> None:
    from cairn.evals.intelligence.dataset import dataset

    specs = dataset()
    settings = config().model_copy(
        update={
            "dataset_sha256": dataset_digest(specs),
            "case_names": tuple(spec.item.case.name for spec in specs),
        }
    )
    settings.settings.check_timeout_seconds = 8
    report = asyncio.run(
        run_experiment(
            specs,
            settings,
            lambda: SequenceLLM([LLMResponse(content="done")]),
            directory=tmp_path / "reports",
            temp_root=tmp_path,
        )
    )
    assert report.state == "completed" and len(report.runs) == 24
    assert len({run.metrics.project_id for run in report.runs}) == 24
    assert all(run.result.status is EvalStatus.FAIL for run in report.runs)
    for spec in specs:
        runs = [run for run in report.runs if run.case_name == spec.item.case.name]
        assert len({run.metrics.initial_tree for run in runs}) == 1
        assert all(
            [check.name for check in run.result.checks if not check.passed]
            == [f"{spec.item.case.name}:behavior"]
            for run in runs
        )
        for run in runs:
            m = run.metrics
            if not run.arm.memory:
                assert not m.memory_fixture_entries and not m.memory_included_events
            elif spec.item.case.name == "stale-batch-history":
                assert m.memory_freshness_counts == {"stale": 1, "unknown": 1}
                assert m.stale_recall_rate == 0 and m.memory_included_events == 0
            elif spec.item.case.name == "unrelated-memory-control":
                assert m.memory_candidates_checked == m.memory_included_events == 0
            else:
                assert (
                    m.memory_included_events == 1 and m.relevant_memory_precision == 1
                )


def test_graph_queries_and_bash_discovery_are_measured_in_loop(tmp_path: Path) -> None:
    def factory() -> SequenceLLM:
        return SequenceLLM(
            [
                LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="graph",
                            name="repo_graph",
                            arguments={"operation": "find_symbols", "name": "parse"},
                        ),
                        ToolCall(id="search", name="bash", arguments={"command": "ls"}),
                    ]
                ),
                LLMResponse(content="done"),
            ]
        )

    report = asyncio.run(
        run_experiment(
            (simple_spec(),),
            config(),
            factory,
            directory=tmp_path / "reports",
            temp_root=tmp_path,
        )
    )
    for run in report.runs:
        assert run.metrics.tool_calls == 2
        assert run.metrics.bash_calls == run.metrics.bash_discovery_calls == 1
        assert run.metrics.graph_query_calls == 1
        assert run.metrics.graph_queries_completed == int(run.arm.graph)
        assert len(run.metrics.direct_discovery_evidence) == int(run.arm.graph)
