"""Case observations through the public Eval and Intelligence entry points."""

import asyncio
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from cairn.core.budget import RunBudget
from cairn.core.context import ContextBudget
from cairn.core.models import LLMResponse, Message, ToolCall
from cairn.infra.observability.tracer import Tracer
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.evals.checks import FileContentEqualsCheck
from cairn.runtime.evals.intelligence.dataset import dataset_digest
from cairn.runtime.evals.intelligence.fixtures import IntelligenceCase, MemoryFixture
from cairn.runtime.evals.intelligence.models import (
    Arm,
    ExperimentConfig,
    ExperimentReport,
    ExperimentRun,
    IntelligenceMetrics,
)
from cairn.runtime.evals.intelligence.reporting import persist
from cairn.runtime.evals.intelligence.runner import (
    IntelligenceRunner,
    SafeTraceSink,
)
from cairn.runtime.evals.models import (
    EvalCase,
    EvalFailureCategory,
    EvalMetrics,
    EvalResult,
    EvalStatus,
    EvalSuite,
    EvalSuiteCase,
    EvalSuiteConfig,
    EvalSuiteResult,
)
from cairn.runtime.evals.suite import EvalSuiteRunner, write_suite_report
from tests.support.runtime import SequenceLLM


def intelligence_case() -> IntelligenceCase:
    files = {"parser.py": "def parse():\n    return 1\n", "other.py": "value = 0\n"}
    return IntelligenceCase(
        EvalSuiteCase(
            EvalCase(name="parser", prompt="Inspect parser contract.", files=files),
            (FileContentEqualsCheck("parser.py", files["parser.py"]),),
        ),
        (MemoryFixture("parser", "Parser exposes parse.", ("parser.py",), True),),
        ("parser.py",),
    )


def read(path: str, call_id: str = "read") -> LLMResponse:
    return LLMResponse(
        tool_calls=[ToolCall(id=call_id, name="read_file", arguments={"path": path})]
    )


def intelligence_runner(
    spec: IntelligenceCase,
    arm: Arm,
    factory: Callable[[], SequenceLLM],
    tmp_path: Path,
) -> IntelligenceRunner:
    observations = IntelligenceMetrics()
    return IntelligenceRunner(
        factory,
        spec=spec,
        arm=arm,
        observations=observations,
        budget=RunBudget(max_steps=4),
        run_timeout_seconds=10,
        check_timeout_seconds=2,
        temp_root=tmp_path,
        tracer=Tracer(SafeTraceSink(tmp_path / "traces", observations)),
    )


@pytest.mark.parametrize("arm", tuple(Arm))
def test_suite_supplied_metrics_keep_each_intelligence_case_isolated(
    tmp_path: Path, arm: Arm
) -> None:
    spec = intelligence_case()
    clients = [
        SequenceLLM([read("parser.py"), LLMResponse(content="first done")]),
        SequenceLLM(
            [
                read("other.py"),
                read("parser.py", "parser"),
                LLMResponse(content="second done"),
            ]
        ),
    ]
    pending = iter(clients)
    runner = intelligence_runner(spec, arm, lambda: next(pending), tmp_path)
    second = EvalSuiteCase(
        spec.item.case.model_copy(update={"name": "parser-second"}), spec.item.checks
    )
    completed: list[dict[str, Any]] = []
    observed: list[IntelligenceMetrics] = []

    def capture(result: EvalResult) -> None:
        completed.append(result.model_dump(mode="json"))
        observed.append(runner.observations.model_copy(deep=True))

    report = asyncio.run(
        EvalSuiteRunner(runner).run(
            EvalSuite("ownership", (spec.item, second)),
            destination=tmp_path / "suite.json",
            on_result=capture,
        )
    )
    assert [result.status for result in report.results] == [EvalStatus.PASS] * 2
    assert [
        result.metrics.request_token_counts_estimated
        for result in report.results
        if result.metrics
    ] == [True, True]
    assert [metrics.read_file_calls for metrics in observed] == [1, 2]
    assert [metrics.provider_requests for metrics in observed] == [2, 3]
    assert [metrics.provider_responses for metrics in observed] == [2, 3]
    assert [len(metrics.request_token_counts) for metrics in observed] == [2, 3]
    assert [
        [item.step for item in metrics.direct_discovery_evidence]
        for metrics in observed
    ] == [[1], [2]]
    assert [len(client.calls) for client in clients] == [2, 3]
    assert [result.model_dump(mode="json") for result in report.results] == completed
    for result, metrics in zip(report.results, observed, strict=True):
        assert isinstance(result.metrics, IntelligenceMetrics)
        assert result.metrics == metrics
        assert result.metrics.agent_elapsed_seconds is not None
        assert result.metrics.memory_included_events == (
            metrics.provider_requests if arm.memory else 0
        )
    assert runner.memory_directory is None
    assert not list(tmp_path.glob("cairn-eval-*"))
    saved = EvalSuiteResult.model_validate_json((tmp_path / "suite.json").read_text())
    assert saved.model_dump(mode="json") == report.model_dump(mode="json")
    # Suite v1 projects metrics onto the explicit base schema. Full Intelligence
    # fields are available in the returned objects and ExperimentReport fields.
    for result in saved.results:
        assert result.metrics is not None
        assert result.metrics.request_token_counts_estimated is True
        assert set(result.metrics.model_dump()) == set(EvalMetrics.model_fields)


@pytest.mark.parametrize("entrypoint", ["run", "run_case"])
def test_completed_result_does_not_change_when_runner_observations_are_reused(
    tmp_path: Path, entrypoint: str
) -> None:
    spec = intelligence_case()
    clients = iter(
        [
            SequenceLLM([read("parser.py"), LLMResponse(content="first done")]),
            SequenceLLM([LLMResponse(content="second done")]),
        ]
    )
    runner = intelligence_runner(spec, Arm.BASELINE, lambda: next(clients), tmp_path)

    async def run() -> EvalResult:
        if entrypoint == "run_case":
            return await runner.run_case()
        return await runner.run(spec.item.case, checks=spec.item.checks)

    first = asyncio.run(run())
    assert isinstance(first.metrics, IntelligenceMetrics)
    before = first.metrics.model_dump(mode="json")
    assert first.metrics.read_file_calls == 1
    second = asyncio.run(run())
    assert isinstance(second.metrics, IntelligenceMetrics)
    assert second.metrics.read_file_calls == 0
    assert first.metrics.read_file_calls == 1
    assert first.metrics.model_dump(mode="json") == before
    assert first.metrics is not second.metrics


@pytest.mark.parametrize("arm", tuple(Arm))
def test_supplied_subclass_metrics_bind_all_observers_to_the_active_case(
    tmp_path: Path, arm: Arm
) -> None:
    from cairn.runtime.evals.intelligence.runner import ObservedLLM

    class ExtendedMetrics(IntelligenceMetrics):
        marker: str = "new case"

    spec = intelligence_case()
    runner = intelligence_runner(
        spec,
        arm,
        lambda: SequenceLLM([read("parser.py"), LLMResponse(content="done")]),
        tmp_path,
    )
    original = runner.observations
    original.provider_requests = 41
    original.read_file_calls = 42
    runner.llm_factory = lambda: ObservedLLM(
        ObservedLLM(
            SequenceLLM([read("parser.py"), LLMResponse(content="done")]), original
        ),
        original,
    )
    before = original.model_dump(mode="json")
    supplied = ExtendedMetrics(provider_requests=99, marker="previous case")
    result = asyncio.run(
        runner.run(spec.item.case, checks=spec.item.checks, metrics=supplied)
    )
    assert result.status is EvalStatus.PASS
    assert runner.observations is supplied
    assert isinstance(result.metrics, ExtendedMetrics)
    assert result.metrics == supplied
    assert result.metrics is not supplied
    assert original.model_dump(mode="json") == before
    assert supplied.marker == "new case"
    assert supplied.request_token_counts_estimated is True
    assert supplied.read_file_calls == 1
    assert supplied.provider_requests == supplied.provider_responses == 2
    assert len(supplied.request_token_counts) == 2
    assert [(item.path, item.step) for item in supplied.direct_discovery_evidence] == [
        ("parser.py", 1)
    ]
    assert supplied.project_id is not None and supplied.initial_tree is not None
    assert supplied.agent_elapsed_seconds is not None
    assert supplied.memory_fixture_entries == int(arm.memory)
    assert supplied.memory_included_events == (2 if arm.memory else 0)
    assert supplied.relevant_memory_precision == (1 if arm.memory else None)
    assert runner.tracer is not None
    assert isinstance(runner.tracer.sink, SafeTraceSink)
    assert runner.tracer.sink.metrics is supplied
    snapshot = result.metrics.model_dump(mode="json")
    supplied.direct_discovery_evidence[0].step = 99
    supplied.request_token_counts.clear()
    supplied.memory_freshness_counts["unknown"] = 99
    assert result.metrics.model_dump(mode="json") == snapshot


def test_incompatible_metrics_fail_before_case_work_or_observer_rebinding(
    tmp_path: Path,
) -> None:
    spec = intelligence_case()
    client = SequenceLLM([LLMResponse(content="unused")])
    runner = intelligence_runner(spec, Arm.MEMORY, lambda: client, tmp_path)
    original = runner.observations
    before = original.model_dump(mode="json")
    supplied = EvalMetrics(model_identifier="keep")
    contents = tuple(tmp_path.iterdir())
    with pytest.raises(TypeError, match="IntelligenceMetrics"):
        asyncio.run(
            runner.run(spec.item.case, checks=spec.item.checks, metrics=supplied)
        )
    assert supplied.model_identifier == "keep"
    assert runner.observations is original
    assert original.model_dump(mode="json") == before
    assert client.calls == []
    assert runner.memory_directory is None
    assert tuple(tmp_path.iterdir()) == contents


@pytest.mark.parametrize("entrypoint", ["run", "run_case"])
def test_both_entrypoints_finalize_memory_precision_and_elapsed_metrics(
    tmp_path: Path, entrypoint: str
) -> None:
    spec = intelligence_case()
    runner = intelligence_runner(
        spec,
        Arm.MEMORY,
        lambda: SequenceLLM([LLMResponse(content="done")]),
        tmp_path,
    )
    result = asyncio.run(
        runner.run_case()
        if entrypoint == "run_case"
        else runner.run(spec.item.case, checks=spec.item.checks)
    )
    assert isinstance(result.metrics, IntelligenceMetrics)
    assert result.metrics == runner.observations
    assert result.metrics.relevant_memory_precision == 1
    assert result.metrics.stale_recall_rate is None
    assert result.metrics.elapsed_seconds is not None
    assert runner.memory_directory is None


@pytest.mark.parametrize("kind", ["empty", "failed-check", "provider-error"])
def test_empty_and_failed_cases_keep_observed_facts_and_finalize(
    tmp_path: Path, kind: str
) -> None:
    class FailingLLM(SequenceLLM):
        async def generate(
            self, messages: list[Message], tools: list[dict[str, Any]] | None = None
        ) -> LLMResponse:
            if not self.responses:
                raise RuntimeError("credential=private request=secret")
            return await super().generate(messages, tools)

    spec = intelligence_case()
    runner = intelligence_runner(
        spec,
        Arm.MEMORY,
        lambda: (
            FailingLLM([read("parser.py")])
            if kind == "provider-error"
            else SequenceLLM([LLMResponse(content="done")])
        ),
        tmp_path,
    )
    checks = (
        ()
        if kind == "empty"
        else (FileContentEqualsCheck("parser.py", "incorrect"),)
        if kind == "failed-check"
        else spec.item.checks
    )
    result = asyncio.run(runner.run(spec.item.case, checks=checks))
    assert isinstance(result.metrics, IntelligenceMetrics)
    metrics = result.metrics
    assert metrics == runner.observations
    assert metrics.elapsed_seconds is not None
    assert runner.memory_directory is None
    assert not list(tmp_path.glob("cairn-eval-*"))
    if kind == "empty":
        assert result.status is EvalStatus.ERROR
        assert metrics.provider_requests == metrics.read_file_calls == 0
        assert metrics.request_token_counts_estimated is None
        assert metrics.relevant_memory_precision is None
    elif kind == "failed-check":
        assert result.status is EvalStatus.FAIL
        assert metrics.provider_requests == metrics.provider_responses == 1
        assert metrics.relevant_memory_precision == 1
    else:
        assert result.status is EvalStatus.ERROR
        assert result.failure_category is EvalFailureCategory.RUNTIME_ERROR
        assert metrics.read_file_calls == 1
        assert metrics.provider_requests == 2
        assert metrics.provider_responses == metrics.provider_failures == 1
        assert metrics.provider_failure_categories == {"unknown": 1}
        assert metrics.direct_discovery_evidence[0].step == 1
        assert metrics.relevant_memory_precision == 1
        assert "private" not in result.model_dump_json()
        assert "secret" not in result.model_dump_json()


def test_overlapping_case_cannot_reset_or_rebind_an_active_runner(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        entered = asyncio.Event()

        class WaitingLLM(SequenceLLM):
            async def generate(
                self, messages: list[Message], tools: list[dict[str, Any]] | None = None
            ) -> LLMResponse:
                entered.set()
                await asyncio.Event().wait()
                return LLMResponse(content="unreachable")

        spec = intelligence_case()
        clients = iter([WaitingLLM([]), SequenceLLM([LLMResponse(content="done")])])
        runner = intelligence_runner(
            spec, Arm.BASELINE, lambda: next(clients), tmp_path
        )
        active = runner.observations
        task = asyncio.create_task(runner.run_case())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            alternate = IntelligenceMetrics(provider_requests=71)
            before = active.model_dump(mode="json")
            with pytest.raises(RuntimeError, match="already running"):
                await runner.run(
                    spec.item.case, checks=spec.item.checks, metrics=alternate
                )
            assert runner.observations is active
            assert active.model_dump(mode="json") == before
            assert alternate.provider_requests == 71
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        result = await runner.run(
            spec.item.case, checks=spec.item.checks, metrics=alternate
        )
        assert result.status is EvalStatus.PASS
        assert alternate.provider_requests == alternate.provider_responses == 1
        assert runner.observations is alternate

    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_repeated_cancel_preserves_completed_and_active_suite_observations(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        cleaning = asyncio.Event()
        release = asyncio.Event()
        settled = asyncio.Event()
        directories: list[Path] = []

        class WaitingAfterRead(SequenceLLM):
            async def generate(
                self, messages: list[Message], tools: list[dict[str, Any]] | None = None
            ) -> LLMResponse:
                if self.responses:
                    return await super().generate(messages, tools)
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    await release.wait()
                    settled.set()
                return LLMResponse(content="unreachable")

        class CapturingRunner(IntelligenceRunner):
            async def _prepare_workspace(
                self, workspace: Workspace, case: EvalCase
            ) -> None:
                directories.append(workspace.root)
                await super()._prepare_workspace(workspace, case)
                assert self.memory_directory is not None
                directories.append(Path(self.memory_directory.name))

        spec = intelligence_case()
        clients = iter(
            [
                SequenceLLM([read("parser.py"), LLMResponse(content="done")]),
                WaitingAfterRead([read("parser.py")]),
            ]
        )
        observations = IntelligenceMetrics()
        runner = CapturingRunner(
            lambda: next(clients),
            spec=spec,
            arm=Arm.BOTH,
            observations=observations,
            budget=RunBudget(max_steps=4),
            run_timeout_seconds=10,
            check_timeout_seconds=2,
            temp_root=tmp_path,
            tracer=Tracer(SafeTraceSink(tmp_path / "traces", observations)),
        )
        second = EvalSuiteCase(
            spec.item.case.model_copy(update={"name": "active"}), spec.item.checks
        )
        completed: list[EvalResult] = []
        destination = tmp_path / "interrupted.json"
        baseline = asyncio.all_tasks()
        task = asyncio.create_task(
            EvalSuiteRunner(runner).run(
                EvalSuite("interrupted", (spec.item, second)),
                destination=destination,
                on_result=completed.append,
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert len(completed) == 1
            first = completed[0].model_dump(mode="json")
            partial = EvalSuiteResult.model_validate_json(destination.read_text())
            assert partial.state == "running" and partial.counts.completed == 1
            assert partial.results[0].model_dump(mode="json") == first
            active = runner.observations
            assert active.provider_requests == 2 and active.provider_responses == 1
            assert active.read_file_calls == 1
            assert active.direct_discovery_evidence[0].step == 1
            task.cancel("original stop")
            await asyncio.wait_for(cleaning.wait(), 5)
            task.cancel("repeated stop")
            release.set()
            with pytest.raises(asyncio.CancelledError, match="original stop"):
                await task
            saved = EvalSuiteResult.model_validate_json(destination.read_text())
            assert saved.state == "interrupted" and saved.counts.completed == 1
            assert saved.active_case_name == "active"
            assert saved.active_case_metrics is not None
            assert saved.active_case_metrics.request_token_counts_estimated is True
            assert saved.active_case_metrics.agent_steps_used == 2
            assert saved.active_case_metrics.elapsed_seconds is not None
            assert set(saved.active_case_metrics.model_dump()) == set(
                EvalMetrics.model_fields
            )
            assert saved.results[0].model_dump(mode="json") == first
            assert completed[0].model_dump(mode="json") == first
            assert active.provider_requests == 2 and active.provider_responses == 1
            assert active.provider_failures == 0
            assert active.read_file_calls == 1
            assert active.relevant_memory_precision == 1
            assert active.agent_elapsed_seconds is not None
            assert settled.is_set()
            assert len(directories) == 4 and all(
                not path.exists() for path in directories
            )
            assert runner.memory_directory is None
            assert asyncio.all_tasks() == baseline
        finally:
            release.set()
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    asyncio.run(asyncio.wait_for(scenario(), 20))


def test_v1_reports_preserve_declared_metrics_and_exclude_unknown_subclass_fields(
    tmp_path: Path,
) -> None:
    class PrivateMetrics(IntelligenceMetrics):
        private_note: str = "private-subclass-data"

    spec = intelligence_case()
    runner = intelligence_runner(
        spec,
        Arm.BASELINE,
        lambda: SequenceLLM([read("parser.py"), LLMResponse(content="done")]),
        tmp_path,
    )
    metrics = PrivateMetrics()
    result = asyncio.run(
        runner.run(spec.item.case, checks=spec.item.checks, metrics=metrics)
    )
    assert isinstance(result.metrics, PrivateMetrics)
    assert result.metrics.read_file_calls == 1
    assert result.metrics.provider_requests == 2
    assert result.metrics.private_note == "private-subclass-data"
    assert "private-subclass-data" not in result.model_dump_json()
    settings = EvalSuiteConfig(
        run_budget=RunBudget(max_steps=4),
        context_budget=ContextBudget(),
        run_timeout_seconds=10,
        check_timeout_seconds=2,
    )
    suite = EvalSuiteResult(
        suite_name="projection",
        case_names=(spec.item.case.name,),
        config=settings,
        state="interrupted",
        results=[result],
        active_case_name="active",
        active_case_metrics=metrics,
    )
    write_suite_report(suite, tmp_path / "projection.json")
    saved_suite = EvalSuiteResult.model_validate_json(
        (tmp_path / "projection.json").read_text()
    )
    assert saved_suite.schema_version == 1
    assert saved_suite.active_case_metrics is not None
    assert saved_suite.active_case_metrics.request_token_counts_estimated is True
    assert set(saved_suite.active_case_metrics.model_dump()) == set(
        EvalMetrics.model_fields
    )
    assert saved_suite.results[0].metrics is not None
    assert saved_suite.results[0].metrics.request_token_counts_estimated is True
    experiment = ExperimentReport(
        config=ExperimentConfig(
            dataset_sha256=dataset_digest((spec,)),
            execution_kind="offline",
            model_identifier="offline-model",
            case_names=(spec.item.case.name,),
            settings=settings,
            sandbox="test",
        ),
        state="interrupted",
        runs=[
            ExperimentRun(
                run_id="case-A",
                case_name=spec.item.case.name,
                arm=Arm.BASELINE,
                repetition=1,
                order_index=0,
                result=result,
                metrics=metrics,
            )
        ],
        active_run_id="active",
        active_metrics=metrics,
    )
    directory = tmp_path / "experiment"
    persist(experiment, directory)
    saved_experiment = ExperimentReport.model_validate_json(
        (directory / "experiment.json").read_text()
    )
    saved_run = ExperimentRun.model_validate_json(
        (directory / "runs" / "case-A.json").read_text()
    )
    assert saved_experiment.schema_version == 1
    assert saved_experiment.active_metrics is not None
    for saved in (
        saved_experiment.active_metrics,
        saved_experiment.runs[0].metrics,
        saved_run.metrics,
    ):
        assert saved.read_file_calls == 1
        assert saved.provider_requests == saved.provider_responses == 2
        assert saved.request_token_counts_estimated is True
        assert saved.direct_discovery_evidence[0].step == 1
        assert set(saved.model_dump()) == set(IntelligenceMetrics.model_fields)
    for path in tmp_path.rglob("*.json"):
        assert "private-subclass-data" not in path.read_text()


def test_cleanup_failure_still_finalizes_observed_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class CleanupFailureRunner(IntelligenceRunner):
        async def _prepare_workspace(
            self, workspace: Workspace, case: EvalCase
        ) -> None:
            await super()._prepare_workspace(workspace, case)
            assert self.memory_directory is not None
            cleanup = self.memory_directory.cleanup

            def fail_after_cleanup() -> None:
                cleanup()
                raise OSError("cleanup failed")

            monkeypatch.setattr(self.memory_directory, "cleanup", fail_after_cleanup)

    runner = CleanupFailureRunner(
        lambda: SequenceLLM([LLMResponse(content="done")]),
        spec=intelligence_case(),
        arm=Arm.MEMORY,
        observations=IntelligenceMetrics(),
        budget=RunBudget(max_steps=2),
        run_timeout_seconds=10,
        check_timeout_seconds=2,
        temp_root=tmp_path,
    )
    with pytest.raises(OSError):
        asyncio.run(runner.run_case())
    assert runner.observations.provider_requests == 1
    assert runner.observations.relevant_memory_precision == 1
    assert runner.observations.elapsed_seconds is not None
    assert runner.memory_directory is None
    assert not list(tmp_path.glob("cairn-eval-*"))
