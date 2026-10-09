import asyncio
import json
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from examples.evals.coding_smoke_cases import coding_smoke_cases, coding_smoke_suite

from cairn.assembly import build_agent
from cairn.core.agent import Agent
from cairn.core.budget import RunBudget
from cairn.core.context import ContextBudget, TokenCount
from cairn.core.models import LLMResponse, Message, ToolCall
from cairn.evals import (
    CheckResult,
    EvalCase,
    EvalCheck,
    EvalMetrics,
    EvalResult,
    EvalRunner,
    EvalStatus,
    EvalSuite,
    EvalSuiteCase,
    EvalSuiteResult,
    EvalSuiteRunner,
    FileContentEqualsCheck,
    FileExistsCheck,
    write_suite_report,
)
from cairn.evals import runner as runner_module
from cairn.evals import suite as suite_module
from cairn.llm.base import LLMClient
from cairn.llm.litellm_client import LiteLLMClient
from cairn.observability.tracer import Tracer
from cairn.workspace.workspace import Workspace
from tests.evals.test_coding_smoke_cases import EXPECTED_NAMES, _expected_files
from tests.support.runtime import RecordingSink, SequenceLLM


def case(name: str) -> EvalSuiteCase:
    return EvalSuiteCase(
        EvalCase(name=name, prompt="Inspect", files={"answer.txt": "old"}),
        (FileExistsCheck("answer.txt"),),
    )


def runner(
    factory: Callable[[], LLMClient], *, temp_root: Path | None = None
) -> EvalRunner:
    return EvalRunner(
        factory,
        budget=RunBudget(max_steps=3),
        run_timeout_seconds=5,
        check_timeout_seconds=2,
        temp_root=temp_root,
    )


def load(path: Path) -> EvalSuiteResult:
    return EvalSuiteResult.model_validate_json(path.read_text())


def test_five_smoke_cases_reuse_original_cases_and_real_eval_loop(
    tmp_path: Path,
) -> None:
    suite = coding_smoke_suite()
    assert tuple(item.case.name for item in suite.cases) == EXPECTED_NAMES
    assert (
        tuple((item.case, item.checks) for item in suite.cases) == coding_smoke_cases()
    )
    clients: list[SequenceLLM] = []
    for item in suite.cases:
        expected = _expected_files(item.case, item.checks)
        edits = [
            ToolCall(
                id=f"edit-{index}",
                name="edit_file",
                arguments={
                    "path": path,
                    "old_text": item.case.files.get(path, ""),
                    "new_text": contents,
                },
            )
            for index, (path, contents) in enumerate(expected.items())
            if item.case.files.get(path) != contents
        ]
        clients.append(
            SequenceLLM(
                ([LLMResponse(tool_calls=edits)] if edits else [])
                + [LLMResponse(content="done")]
            )
        )
    pending = iter(clients)
    printed: list[EvalResult] = []
    destination = tmp_path / "report.json"
    report = asyncio.run(
        EvalSuiteRunner(runner(lambda: next(pending))).run(
            suite, destination=destination, on_result=printed.append
        )
    )
    assert report.state == "completed"
    assert report.counts.model_dump() == {
        "total": 5,
        "completed": 5,
        "passed": 5,
        "failed": 0,
        "errors": 0,
    }
    assert [result.case_name for result in report.results] == list(EXPECTED_NAMES)
    assert all(result.status is EvalStatus.PASS for result in report.results)
    assert printed == report.results
    assert load(destination) == report
    assert json.loads(report.model_dump_json())["counts"]["passed"] == 5
    assert all(client.calls for client in clients)


def test_claims_and_later_errors_do_not_stop_suite_or_replace_checks(
    tmp_path: Path,
) -> None:
    clients = iter(
        [
            SequenceLLM([LLMResponse(content="fine")]),
            SequenceLLM([LLMResponse(content="I fixed everything; all tests pass")]),
            SequenceLLM([]),
            SequenceLLM([LLMResponse(content="fine")]),
        ]
    )
    failing = EvalSuiteCase(
        case("claimed-fix").case, (FileContentEqualsCheck("answer.txt", "new"),)
    )
    suite = EvalSuite("mixed", (case("first"), failing, case("error"), case("last")))
    path = tmp_path / "mixed.json"
    observed: list[int] = []

    def completed(result: EvalResult) -> None:
        saved = load(path)
        assert saved.results[-1] == result
        observed.append(saved.counts.completed)

    report = asyncio.run(
        EvalSuiteRunner(runner(lambda: next(clients))).run(
            suite, destination=path, on_result=completed
        )
    )
    assert [result.status for result in report.results] == [
        EvalStatus.PASS,
        EvalStatus.FAIL,
        EvalStatus.ERROR,
        EvalStatus.PASS,
    ]
    assert report.results[1].checks[0].passed is False
    assert report.results[2].error == "Case execution failed."
    assert (
        report.counts.passed == 2 and report.counts.failed == report.counts.errors == 1
    )
    assert observed == [1, 2, 3, 4]
    assert report.active_case_name is None


def test_suite_validation() -> None:
    with pytest.raises(ValueError, match="nonempty"):
        EvalSuite("empty", ())
    with pytest.raises(ValueError, match="nonempty"):
        EvalSuite(" ", (case("valid"),))
    with pytest.raises(ValueError, match="unique"):
        EvalSuite("duplicate", (case("same"), case("same")))
    with pytest.raises(ValueError, match="checks"):
        EvalSuiteCase(case("no-checks").case, ())
    with pytest.raises(ValueError, match="nonempty"):
        EvalSuiteCase(EvalCase(name=" ", prompt="inspect"), (FileExistsCheck("x"),))


def test_atomic_replacement_preserves_existing_report_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "report.json"
    report = asyncio.run(
        EvalSuiteRunner(runner(lambda: SequenceLLM([LLMResponse(content="done")]))).run(
            EvalSuite("atomic", (case("one"),)), destination=path
        )
    )
    original = path.read_bytes()

    def fail_replace(source: Path, destination: Path) -> None:
        assert destination == path
        assert source.parent == path.parent
        assert path.read_bytes() == original
        assert json.loads(source.read_text())["schema_version"] == 1
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        write_suite_report(report, path)
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_persistence_failure_stops_before_next_case_and_keeps_prior_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "report.json"
    saves = 0
    factories = 0

    def factory() -> LLMClient:
        nonlocal factories
        factories += 1
        return SequenceLLM([LLMResponse(content="done")])

    def save(report: EvalSuiteResult, destination: Path) -> None:
        nonlocal saves
        saves += 1
        if saves == 3:
            raise OSError("later report write failed")
        write_suite_report(report, destination)

    monkeypatch.setattr(suite_module, "write_suite_report", save)
    with pytest.raises(OSError, match="later report write failed"):
        asyncio.run(
            EvalSuiteRunner(runner(factory)).run(
                EvalSuite("writes", (case("first"), case("second"), case("never"))),
                destination=path,
            )
        )
    assert factories == 2
    assert [result.case_name for result in load(path).results] == ["first"]
    assert load(path).state == "running"


class ExactCounter:
    def count(self, messages: list[Message], tools: list[dict[str, Any]]) -> TokenCount:
        return TokenCount(tokens=10, is_estimate=False)


def test_real_trace_usage_and_explicit_provider_limit_are_distinct_from_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cairn.llm import litellm_client

    completion = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="done", tool_calls=None)
                )
            ],
            usage=SimpleNamespace(prompt_tokens=41, completion_tokens=7),
        )
    )
    monkeypatch.setattr(litellm_client, "acompletion", completion)
    sink = RecordingSink()
    case_runner = EvalRunner(
        lambda: LiteLLMClient("fake/model", api_key="FAKE_SECRET", max_output_tokens=9),
        budget=RunBudget(max_steps=3),
        context_budget=ContextBudget(max_tokens=256, response_tokens=32),
        run_timeout_seconds=5,
        check_timeout_seconds=2,
        tracer=Tracer(sink),
        token_counter=ExactCounter(),
    )
    report = asyncio.run(
        EvalSuiteRunner(case_runner).run(
            EvalSuite("metrics", (case("one"),)), destination=tmp_path / "metrics.json"
        )
    )
    metrics = report.results[0].metrics
    assert metrics is not None
    assert metrics.model_identifier == "fake/model"
    assert (
        metrics.trace_id
        == report.results[0].trace_id
        == sink.spans[-1].context.trace_id
    )
    assert metrics.agent_steps_used == 1
    assert metrics.elapsed_seconds is not None and metrics.elapsed_seconds > 0
    assert metrics.context_trimmed_turns == metrics.context_trimmed_messages == 0
    assert metrics.provider_input_tokens == 41
    assert metrics.provider_output_tokens == 7
    assert sink.spans[-1].attributes["input_tokens"] == 41
    assert metrics.request_token_counts_estimated is False
    assert (
        metrics.token_counter_implementation
        == f"{ExactCounter.__module__}.ExactCounter"
    )
    assert metrics.provider_max_output_tokens == 9
    assert completion.call_args.kwargs["max_tokens"] == 9
    assert report.config.context_budget.response_tokens == 32
    assert report.config.run_budget.max_steps == 3
    assert report.config.run_timeout_seconds == 5
    assert report.config.check_timeout_seconds == 2
    assert "FAKE_SECRET" not in report.model_dump_json()


def test_missing_metadata_stays_null_and_estimates_are_not_usage(
    tmp_path: Path,
) -> None:
    report = asyncio.run(
        EvalSuiteRunner(runner(lambda: SequenceLLM([LLMResponse(content="done")]))).run(
            EvalSuite("unknown", (case("one"),)), destination=tmp_path / "unknown.json"
        )
    )
    metrics = report.results[0].metrics
    assert metrics is not None
    assert metrics.trace_id is None
    assert metrics.model_identifier is None
    assert metrics.provider_input_tokens is metrics.provider_output_tokens is None
    assert metrics.provider_max_output_tokens is None
    assert metrics.request_token_counts_estimated is True
    assert (
        metrics.token_counter_implementation
        == "cairn.core.context.EstimatedTokenCounter"
    )


def test_failure_before_runtime_has_no_invented_metrics(tmp_path: Path) -> None:
    def factory() -> LLMClient:
        raise RuntimeError("provider secret and request payload must not be serialized")

    report = asyncio.run(
        EvalSuiteRunner(runner(factory)).run(
            EvalSuite("early-error", (case("one"),)),
            destination=tmp_path / "early.json",
        )
    )
    metrics = report.results[0].metrics
    assert metrics is not None
    assert metrics.agent_steps_used is None
    assert metrics.request_token_counts_estimated is None
    assert metrics.token_counter_implementation is None
    assert metrics.context_trimmed_turns is None
    assert metrics.provider_input_tokens is None
    assert metrics.provider_output_tokens is None
    assert "request payload" not in report.model_dump_json()


@pytest.mark.parametrize("stage", ["model", "check"])
@pytest.mark.parametrize("repeat", [False, True])
def test_external_cancellation_persists_interruption_after_workspace_cleanup(
    tmp_path: Path, stage: str, repeat: bool
) -> None:
    async def scenario() -> None:
        parent = tmp_path / "workspaces"
        parent.mkdir()
        sentinel = parent / "keep.txt"
        sentinel.write_text("caller owned")
        started = asyncio.Event()
        cleaning = asyncio.Event()
        release = asyncio.Event()
        settled = asyncio.Event()
        calls = 0

        class SlowLLM:
            async def generate(
                self, messages: list[Message], tools: list[dict[str, Any]] | None = None
            ) -> LLMResponse:
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    await release.wait()
                    settled.set()
                return LLMResponse(content="unreachable")

        class SlowCheck:
            name = "slow-check"

            async def evaluate(self, workspace: Workspace) -> CheckResult:
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    await release.wait()
                    settled.set()
                return CheckResult(name=self.name, passed=True)

        def factory() -> LLMClient:
            nonlocal calls
            calls += 1
            if calls == 2 and stage == "model":
                return SlowLLM()
            return SequenceLLM([LLMResponse(content="done")])

        active = case("active")
        if stage == "check":
            active = EvalSuiteCase(active.case, (SlowCheck(),))
        case_runner = runner(factory, temp_root=parent)
        case_runner.tracer = Tracer(RecordingSink())
        path = tmp_path / "interrupted.json"
        baseline = asyncio.all_tasks()
        task = asyncio.create_task(
            EvalSuiteRunner(case_runner).run(
                EvalSuite("cancel", (case("first"), active, case("never"))),
                destination=path,
            )
        )
        await asyncio.wait_for(started.wait(), 3)
        assert [result.case_name for result in load(path).results] == ["first"]
        workspaces = list(parent.glob("cairn-eval-*"))
        assert len(workspaces) == 1
        task.cancel("original caller cancellation")
        await asyncio.wait_for(cleaning.wait(), 3)
        assert not task.done()
        if repeat:
            task.cancel("repeated cancellation")
        release.set()
        with pytest.raises(
            asyncio.CancelledError, match="original caller cancellation"
        ):
            await task
        report = load(path)
        assert report.state == "interrupted"
        assert report.active_case_name == "active"
        assert report.active_case_metrics is not None
        assert report.active_case_metrics.elapsed_seconds is not None
        assert report.active_case_metrics.trace_id is not None
        assert [result.case_name for result in report.results] == ["first"]
        assert report.counts.completed == 1
        assert report.counts.total == 3
        assert calls == 2
        assert settled.is_set()
        assert not workspaces[0].exists()
        assert list(parent.iterdir()) == [sentinel]
        assert asyncio.all_tasks() == baseline

    asyncio.run(asyncio.wait_for(scenario(), 6))


def test_cancellation_report_failure_is_observable_without_masking_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        parent = tmp_path / "workspaces"
        parent.mkdir()

        class SlowLLM:
            async def generate(
                self, messages: list[Message], tools: list[dict[str, Any]] | None = None
            ) -> LLMResponse:
                started.set()
                await asyncio.Event().wait()
                return LLMResponse(content="unreachable")

        def save(report: EvalSuiteResult, destination: Path) -> None:
            if report.state == "interrupted":
                raise OSError("cancel write failed")
            write_suite_report(report, destination)

        monkeypatch.setattr(suite_module, "write_suite_report", save)
        task = asyncio.create_task(
            EvalSuiteRunner(runner(SlowLLM, temp_root=parent)).run(
                EvalSuite("cancel-write", (case("active"),)),
                destination=tmp_path / "report.json",
            )
        )
        await started.wait()
        task.cancel("original cancellation")
        with pytest.raises(
            asyncio.CancelledError, match="original cancellation"
        ) as caught:
            await task
        assert "persistence failed: OSError" in " ".join(caught.value.__notes__)
        assert list(parent.iterdir()) == []

    asyncio.run(asyncio.wait_for(scenario(), 6))


def test_completed_case_racing_with_cancel_is_not_lost(tmp_path: Path) -> None:
    async def scenario() -> None:
        caller = asyncio.current_task()
        assert caller is not None

        class FinishingRunner(EvalRunner):
            async def run(
                self,
                case: EvalCase,
                *,
                checks: Sequence[EvalCheck],
                metrics: EvalMetrics | None = None,
            ) -> EvalResult:
                result = await super().run(case, checks=checks, metrics=metrics)
                assert caller is not None
                caller.cancel("cancel as case completes")
                return result

        parent = tmp_path / "workspaces"
        parent.mkdir()
        case_runner = FinishingRunner(
            lambda: SequenceLLM([LLMResponse(content="done")]),
            budget=RunBudget(max_steps=1),
            run_timeout_seconds=5,
            check_timeout_seconds=2,
            temp_root=parent,
        )
        destination = tmp_path / "race.json"
        with pytest.raises(asyncio.CancelledError, match="cancel as case completes"):
            await EvalSuiteRunner(case_runner).run(
                EvalSuite("race", (case("completed"), case("never"))),
                destination=destination,
            )
        report = load(destination)
        assert report.state == "interrupted"
        assert report.counts.completed == report.counts.passed == 1
        assert report.results[0].case_name == "completed"
        assert report.active_case_name is None
        assert list(parent.iterdir()) == []

    asyncio.run(scenario())


def test_injected_cancel_keeps_original_exception_and_notes(tmp_path: Path) -> None:
    cancellation = asyncio.CancelledError("injected cancellation")
    cancellation.add_note("upstream cancellation diagnostic")

    class CancelledLLM:
        async def generate(
            self, messages: list[Message], tools: list[dict[str, Any]] | None = None
        ) -> LLMResponse:
            raise cancellation

    async def scenario() -> None:
        with pytest.raises(asyncio.CancelledError) as caught:
            await EvalSuiteRunner(runner(CancelledLLM)).run(
                EvalSuite("injected", (case("active"),)),
                destination=tmp_path / "cancel.json",
            )
        assert caught.value is cancellation
        assert caught.value.__notes__ == ["upstream cancellation diagnostic"]
        assert load(tmp_path / "cancel.json").results == []

    asyncio.run(scenario())


def test_partial_provider_usage_is_not_invented(tmp_path: Path) -> None:
    from cairn.core.models import LLMUsage

    case_runner = runner(
        lambda: SequenceLLM(
            [LLMResponse(content="done", usage=LLMUsage(input_tokens=11))]
        )
    )
    case_runner.tracer = Tracer(RecordingSink())
    report = asyncio.run(
        EvalSuiteRunner(case_runner).run(
            EvalSuite("partial", (case("one"),)), destination=tmp_path / "partial.json"
        )
    )
    metrics = report.results[0].metrics
    assert metrics is not None
    assert metrics.provider_input_tokens == 11
    assert metrics.provider_output_tokens is None


def test_context_omissions_match_actual_request_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class CharacterCounter:
        def count(
            self, messages: list[Message], tools: list[dict[str, Any]]
        ) -> TokenCount:
            return TokenCount(
                tokens=sum(len(message.content or "") for message in messages),
                is_estimate=True,
            )

    def seeded_agent(**kwargs: Any) -> Agent:
        agent = build_agent(**kwargs)
        agent.system_prompt = "system"
        agent.state.add_user_message("old context " * 200)
        agent.state.add_assistant_message("old answer " * 200)
        return agent

    monkeypatch.setattr(runner_module, "build_agent", seeded_agent)
    sink = RecordingSink()
    case_runner = EvalRunner(
        lambda: SequenceLLM([LLMResponse(content="done")]),
        budget=RunBudget(max_steps=1),
        context_budget=ContextBudget(max_tokens=512, response_tokens=32),
        run_timeout_seconds=5,
        check_timeout_seconds=2,
        tracer=Tracer(sink),
        token_counter=CharacterCounter(),
    )
    report = asyncio.run(
        EvalSuiteRunner(case_runner).run(
            EvalSuite("trim", (case("one"),)), destination=tmp_path / "trim.json"
        )
    )
    metrics = report.results[0].metrics
    assert metrics is not None
    assert report.results[0].status is EvalStatus.PASS
    assert metrics.context_trimmed_turns == 1
    assert metrics.context_trimmed_messages == 2
    request_span = next(span for span in sink.spans if span.name == "llm.generate")
    assert (
        request_span.attributes["context_omitted_turns"]
        == metrics.context_trimmed_turns
    )
    assert (
        request_span.attributes["context_omitted_messages"]
        == metrics.context_trimmed_messages
    )
    assert metrics.request_token_counts_estimated is True
    assert "old context" not in report.model_dump_json()


def test_reused_metrics_do_not_keep_previous_case_observations() -> None:
    async def scenario() -> None:
        metrics = EvalMetrics(model_identifier="previous", provider_input_tokens=99)
        result = await runner(lambda: SequenceLLM([LLMResponse(content="done")])).run(
            case("new").case, checks=case("new").checks, metrics=metrics
        )
        assert result.metrics is metrics
        assert metrics.model_identifier is None
        assert metrics.provider_input_tokens is None
        assert metrics.elapsed_seconds is not None

    asyncio.run(scenario())


def test_report_contains_no_provider_errors_or_conversation_payloads(
    tmp_path: Path,
) -> None:
    secret = "FAKE_PROVIDER_CREDENTIAL"
    payload = "PRIVATE_CONVERSATION_PAYLOAD"

    class FailingLLM:
        api_key = secret

        async def generate(
            self, messages: list[Message], tools: list[dict[str, Any]] | None = None
        ) -> LLMResponse:
            raise RuntimeError(f"{secret}: {payload}")

    class LeakyCheck:
        name = "diagnostic-check"

        async def evaluate(self, workspace: Workspace) -> CheckResult:
            return CheckResult(
                name=self.name, passed=False, message=payload, error=secret
            )

    path = tmp_path / "safe.json"
    report = asyncio.run(
        EvalSuiteRunner(runner(FailingLLM)).run(
            EvalSuite(
                "safe",
                (EvalSuiteCase(EvalCase(name="one", prompt=payload), (LeakyCheck(),)),),
            ),
            destination=path,
        )
    )
    assert report.results[0].status is EvalStatus.ERROR
    assert secret not in path.read_text()
    assert payload not in path.read_text()
    assert report.results[0].checks[0].error == "Check evaluation failed."


@pytest.mark.parametrize("success", [True, False])
def test_manual_script_uses_suite_and_returns_aggregate_exit_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    success: bool,
) -> None:
    from examples.evals import run_coding_smoke

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        run_coding_smoke,
        "resolve_cairn_config",
        lambda *_: {
            "CAIRN_LLM_MODEL": "fake/model",
            "CAIRN_LLM_API_KEY": "FAKE_SECRET",
            "CAIRN_BASE_URL": "https://example.invalid",
        },
    )
    monkeypatch.setattr(
        run_coding_smoke,
        "LiteLLMClient",
        lambda **_: SequenceLLM([LLMResponse(content="everything is correct")]),
    )
    chosen = case("script")
    if not success:
        chosen = EvalSuiteCase(
            chosen.case, (FileContentEqualsCheck("answer.txt", "new"),)
        )
    monkeypatch.setattr(
        run_coding_smoke, "coding_smoke_suite", lambda: EvalSuite("script", (chosen,))
    )
    path = tmp_path / "script.json"
    code = asyncio.run(run_coding_smoke.main(path))
    assert code == (0 if success else 1)
    assert load(path).state == "completed"
    output = capsys.readouterr().out
    assert f"{'PASS' if success else 'FAIL'}" in output
    assert (
        "1 passed, 0 failed, 0 errors" in output
        if success
        else "0 passed, 1 failed, 0 errors" in output
    )
    assert str(path) in output
    assert "FAKE_SECRET" not in path.read_text()
