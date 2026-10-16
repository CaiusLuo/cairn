import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest

from cairn.core.budget import RunBudget
from cairn.core.context import ContextBudget
from cairn.core.llm import LLMClient
from cairn.core.models import LLMResponse, LLMUsage, Message, ToolCall
from cairn.infra.observability.sinks import JsonlTraceSink
from cairn.infra.observability.storage import TraceStore
from cairn.infra.observability.tracer import Tracer
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.evals import CheckResult, FileContentEqualsCheck
from cairn.runtime.tasks import (
    CodingTaskRunner,
    TaskExecutionSummary,
    TaskSpec,
    TaskStatus,
    summarize_task,
)
from cairn.runtime.tasks import metrics as metrics_module
from cairn.runtime.tasks import runner as runner_module
from tests.support.durable_tasks import make_host
from tests.support.runtime import FailingLLM, FailingSink, SequenceLLM

SPEC = TaskSpec(task_id="issue-48-offline", prompt="Inspect the local file")
SECRET = "credential-value-never-export-this"


def make_runner(
    root: Path, llm: LLMClient, *, tracer: Tracer | None = None
) -> CodingTaskRunner:
    work = root / "work"
    work.mkdir(exist_ok=True)
    return CodingTaskRunner(
        workspace=Workspace(work), llm=llm, budget=RunBudget(max_steps=4), tracer=tracer
    )


def require_summary(runner: CodingTaskRunner) -> TaskExecutionSummary:
    summary = runner.last_summary
    assert summary is not None
    assert len(summary.to_json().encode()) <= metrics_module.MAX_SUMMARY_BYTES
    return summary


def test_completed_is_not_verified_and_actual_checks_are_explicit(
    tmp_path: Path,
) -> None:
    llm = SequenceLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="read", name="read_file", arguments={"path": "local.txt"}
                    )
                ],
                usage=LLMUsage(input_tokens=11, output_tokens=2),
            ),
            LLMResponse(
                content="I claim it is correct",
                usage=LLMUsage(input_tokens=13, output_tokens=3),
            ),
        ]
    )
    runner = make_runner(
        tmp_path, llm, tracer=Tracer(JsonlTraceSink(tmp_path / "traces"))
    )
    (runner.workspace.root / "local.txt").write_text("actual\n")
    result = asyncio.run(runner.run(SPEC))
    base = require_summary(runner)
    assert (
        base.task_id == SPEC.task_id
        and base.status is result.status is TaskStatus.COMPLETED
    )
    assert base.trace_id == result.trace_id
    assert (
        base.result_available
        and base.elapsed_seconds is not None
        and base.elapsed_seconds >= 0
    )
    assert base.session_id is base.turn_id is None
    assert base.verification_verdict is None and base.verification_checks is None
    assert base.model_attempts is base.tool_attempts is None
    assert (base.provider_input_tokens, base.provider_output_tokens) == (24, 5)
    assert base.cost_usd is None
    actual = asyncio.run(
        FileContentEqualsCheck("local.txt", "actual\n").evaluate(runner.workspace)
    )
    wrong = asyncio.run(
        FileContentEqualsCheck("local.txt", "different\n").evaluate(runner.workspace)
    )
    summary = summarize_task(
        base, trace_store=TraceStore(tmp_path / "traces"), checks=[actual]
    )
    assert summary.model_attempts == 2
    assert summary.tool_attempts == summary.tool_executions == 1
    assert summary.tool_failures == summary.permission_denials == 0
    assert summary.failure_category is None and summary.trace_diagnostic is None
    assert summary.verification_verdict == "pass" and summary.verification_checks == 1
    assert summarize_task(base, checks=[wrong]).verification_verdict == "fail"
    assert (
        summarize_task(
            base, checks=[CheckResult(name=SECRET, passed=False, error=SECRET)]
        ).verification_verdict
        == "error"
    )
    assert summarize_task(base, checks=[]).verification_verdict is None
    assert runner.last_summary is base and base.verification_verdict is None
    assert set(result.model_dump()) == {
        "task_id",
        "status",
        "repository",
        "final_response",
        "trace_id",
        "error",
    }
    assert TaskExecutionSummary.model_validate_json(summary.to_json()) == summary


def test_model_failure_is_classified_without_exception_text(tmp_path: Path) -> None:
    runner = make_runner(
        tmp_path, FailingLLM(), tracer=Tracer(JsonlTraceSink(tmp_path / "traces"))
    )
    result = asyncio.run(runner.run(SPEC))
    summary = summarize_task(
        require_summary(runner), trace_store=TraceStore(tmp_path / "traces")
    )
    assert result.status is TaskStatus.RUNTIME_ERROR
    assert (
        summary.status is result.status and summary.failure_category == "runtime_error"
    )
    assert summary.model_failures == 1
    assert summary.model_attempts == 1 and summary.tool_attempts == 0
    assert (
        summary.provider_input_tokens
        is summary.provider_output_tokens
        is summary.cost_usd
        is None
    )
    assert "llm failed" not in summary.to_json()


@pytest.mark.parametrize("error_type", [RuntimeError, TimeoutError])
def test_exception_secrets_and_typed_timeout(
    tmp_path: Path, error_type: type[Exception]
) -> None:
    class SecretFailure:
        async def generate(
            self,
            messages: list[Message],
            tools: list[dict[str, Any]] | None = None,
        ) -> LLMResponse:
            raise error_type(SECRET)

    runner = make_runner(
        tmp_path,
        SecretFailure(),
        tracer=Tracer(JsonlTraceSink(tmp_path / "traces")),
    )
    result = asyncio.run(runner.run(SPEC))
    summary = summarize_task(
        require_summary(runner), trace_store=TraceStore(tmp_path / "traces")
    )
    assert result.error is not None and SECRET in result.error
    assert SECRET not in summary.to_json()
    assert summary.failure_category == (
        "timeout" if error_type is TimeoutError else "runtime_error"
    )
    assert summary.timeout_observed == (True if error_type is TimeoutError else None)


def test_admission_budget_and_pre_cancel_do_not_invent_provider_usage(
    tmp_path: Path,
) -> None:
    runner = make_runner(
        tmp_path,
        SequenceLLM([]),
        tracer=Tracer(JsonlTraceSink(tmp_path / "traces")),
    )
    runner.context_builder.budget = ContextBudget(max_tokens=2, response_tokens=1)
    result = asyncio.run(runner.run(SPEC))
    summary = summarize_task(
        require_summary(runner), trace_store=TraceStore(tmp_path / "traces")
    )
    assert result.status is TaskStatus.BUDGET_EXHAUSTED
    assert summary.failure_category == "budget_exhausted"
    assert summary.model_attempts == 0  # Complete trace proves no admitted request.
    assert summary.provider_input_tokens is summary.provider_output_tokens is None

    async def pre_cancel() -> None:
        cancellation = asyncio.Event()
        cancellation.set()
        result = await runner.run(SPEC, cancellation_event=cancellation)
        summary = require_summary(runner)
        assert result.status is summary.status is TaskStatus.CANCELLED
        assert summary.trace_id is None
        assert summary.turn_id is None and summary.session_id is None
        assert summary.model_attempts is summary.provider_input_tokens is None

    asyncio.run(pre_cancel())


@pytest.mark.parametrize("tool", ["read_file", "does_not_exist", "bash"])
def test_failed_or_denied_tool_does_not_fabricate_task_failure(
    tmp_path: Path, tool: str
) -> None:
    arguments = (
        {
            "command": "must-never-execute",
            "network_access": True,
            "justification": "denied offline",
        }
        if tool == "bash"
        else {"path": "missing.txt"}
    )
    runner = make_runner(
        tmp_path,
        SequenceLLM(
            [
                LLMResponse(
                    tool_calls=[ToolCall(id="tool", name=tool, arguments=arguments)]
                ),
                LLMResponse(content="runtime continued"),
            ]
        ),
        tracer=Tracer(JsonlTraceSink(tmp_path / "traces")),
    )
    result = asyncio.run(runner.run(SPEC))
    summary = summarize_task(
        require_summary(runner), trace_store=TraceStore(tmp_path / "traces")
    )
    assert result.status is summary.status is TaskStatus.COMPLETED
    assert summary.failure_category is None
    assert summary.model_attempts == 2 and summary.tool_attempts == 1
    assert summary.tool_executions == (1 if tool == "read_file" else 0)
    assert summary.tool_failures == (0 if tool == "bash" else 1)
    assert summary.permission_denials == (1 if tool == "bash" else 0)
    assert summary.verification_verdict is None


@pytest.mark.parametrize(
    "usage", [None, LLMUsage(input_tokens=0, output_tokens=0), LLMUsage(input_tokens=7)]
)
@pytest.mark.parametrize("tracing", [False, True])
def test_unknown_usage_is_not_zero(
    tmp_path: Path, usage: LLMUsage | None, tracing: bool
) -> None:
    runner = make_runner(
        tmp_path,
        SequenceLLM([LLMResponse(content="done", usage=usage)]),
        tracer=Tracer(JsonlTraceSink(tmp_path / "traces")) if tracing else None,
    )
    asyncio.run(runner.run(SPEC))
    summary = require_summary(runner)
    assert summary.provider_input_tokens == (
        usage.input_tokens if usage and tracing else None
    )
    assert summary.provider_output_tokens == (
        usage.output_tokens if usage and tracing else None
    )
    assert summary.cost_usd is None
    assert summary.model_attempts is None


def test_later_failed_model_attempt_invalidates_earlier_usage(tmp_path: Path) -> None:
    runner = make_runner(
        tmp_path,
        SequenceLLM(
            [
                LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="read", name="read_file", arguments={"path": "missing"}
                        )
                    ],
                    usage=LLMUsage(input_tokens=20, output_tokens=2),
                )
            ]
        ),
        tracer=Tracer(JsonlTraceSink(tmp_path / "traces")),
    )
    result = asyncio.run(
        runner.run(SPEC)
    )  # Script exhaustion is a second, failed call.
    summary = summarize_task(
        require_summary(runner), trace_store=TraceStore(tmp_path / "traces")
    )
    assert result.status is TaskStatus.RUNTIME_ERROR and summary.model_attempts == 2
    assert summary.failure_category == "runtime_error" and summary.model_failures == 1
    assert summary.provider_input_tokens is summary.provider_output_tokens is None


@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("broken_sink", [False, True])
def test_cancellation_summary_follows_settlement_and_preserves_propagation(
    tmp_path: Path, external: bool, broken_sink: bool
) -> None:
    async def scenario() -> None:
        started, cleaning, release, settled = (asyncio.Event() for _ in range(4))
        cancellation = asyncio.Event()

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

        tracer = Tracer(
            FailingSink() if broken_sink else JsonlTraceSink(tmp_path / "traces")
        )
        runner = make_runner(tmp_path, SlowLLM(), tracer=tracer)
        baseline = asyncio.all_tasks()
        execution = asyncio.create_task(
            runner.run(SPEC, cancellation_event=cancellation)
        )
        await asyncio.wait_for(started.wait(), 2)
        if external:
            execution.cancel("original caller cancellation")
        else:
            cancellation.set()
        await asyncio.wait_for(cleaning.wait(), 2)
        assert runner.last_summary is None
        if external:
            execution.cancel("repeated caller cancellation")
        release.set()
        if external:
            with pytest.raises(
                asyncio.CancelledError, match="original caller cancellation"
            ):
                await execution
        else:
            result = await execution
            assert result.status is TaskStatus.CANCELLED
        assert settled.is_set() and asyncio.all_tasks() == baseline
        summary = summarize_task(
            require_summary(runner), trace_store=TraceStore(tmp_path / "traces")
        )
        assert (
            summary.status is TaskStatus.CANCELLED
            and summary.failure_category == "cancelled"
        )
        assert summary.result_available is not external
        assert summary.cancellation_propagated is external
        assert summary.elapsed_seconds is not None
        assert summary.model_attempts == (None if broken_sink else 1)
        assert summary.provider_input_tokens is summary.provider_output_tokens is None
        assert summary.trace_persisted is not broken_sink

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_late_cancellation_preserves_an_already_produced_task_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        runner = make_runner(tmp_path, SequenceLLM([LLMResponse(content="done")]))
        original = runner._execute

        async def finish_then_cancel(*args: Any, **kwargs: Any) -> Any:
            result = await original(*args, **kwargs)
            execution.cancel("late delivery cancellation")
            return result

        monkeypatch.setattr(runner, "_execute", finish_then_cancel)
        execution = asyncio.create_task(runner.run(SPEC))
        with pytest.raises(asyncio.CancelledError, match="late delivery cancellation"):
            await execution
        summary = require_summary(runner)
        assert summary.cancellation_propagated and summary.result_available
        assert summary.status is TaskStatus.COMPLETED
        assert summary.failure_category is None

    asyncio.run(scenario())


def test_trace_sink_failure_keeps_result_and_known_usage(tmp_path: Path) -> None:
    results = []
    for sink in (JsonlTraceSink(tmp_path / "traces"), FailingSink()):
        runner = make_runner(
            tmp_path,
            SequenceLLM(
                [
                    LLMResponse(
                        content="done", usage=LLMUsage(input_tokens=12, output_tokens=3)
                    )
                ]
            ),
            tracer=Tracer(sink),
        )
        results.append(asyncio.run(runner.run(SPEC)))
        summary = summarize_task(
            require_summary(runner), trace_store=TraceStore(tmp_path / "traces")
        )
        assert (summary.provider_input_tokens, summary.provider_output_tokens) == (
            12,
            3,
        )
        assert summary.status is TaskStatus.COMPLETED
        if isinstance(sink, FailingSink):
            assert (
                summary.trace_persisted is False
                and summary.trace_diagnostic == "sink_failed"
            )
            assert (
                summary.model_attempts
                is summary.tool_attempts
                is summary.permission_denials
                is None
            )
    assert results[0].model_dump(exclude={"trace_id"}) == results[1].model_dump(
        exclude={"trace_id"}
    )


@pytest.mark.parametrize("failure", ["construction", "observe", "finish", "clock"])
def test_metrics_failure_is_not_an_execution_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(SECRET)

    if failure == "clock":
        monkeypatch.setattr(runner_module, "perf_counter", fail)
    else:
        monkeypatch.setattr(
            metrics_module._TaskObservation,
            "__init__" if failure == "construction" else failure,
            fail,
        )
    runner = make_runner(tmp_path, SequenceLLM([LLMResponse(content="same result")]))
    result = asyncio.run(runner.run(SPEC))
    assert (
        result.status is TaskStatus.COMPLETED and result.final_response == "same result"
    )
    assert result.error is None
    if failure in ("construction", "finish"):
        assert runner.last_summary is None
    else:
        summary = require_summary(runner)
        assert SECRET not in summary.to_json()
        if failure == "clock":
            assert summary.elapsed_seconds is None


@pytest.mark.parametrize(
    "damage",
    ["missing", "no_root", "corrupt", "foreign_id", "duplicate", "unfinished_child"],
)
def test_unavailable_trace_never_becomes_zero_calls(
    tmp_path: Path, damage: str
) -> None:
    trace_root = tmp_path / "traces"
    runner = make_runner(
        tmp_path,
        SequenceLLM([LLMResponse(content="done")]),
        tracer=Tracer(JsonlTraceSink(trace_root)),
    )
    result = asyncio.run(runner.run(SPEC))
    path = trace_root / f"{result.trace_id}.jsonl"
    previously_observed = summarize_task(
        require_summary(runner), trace_store=TraceStore(trace_root)
    )
    assert previously_observed.model_attempts == 1
    records = [json.loads(line) for line in path.read_text().splitlines()]
    if damage == "missing":
        path.unlink()
    else:
        if damage == "no_root":
            records = [r for r in records if r["context"]["parent_span_id"] is not None]
        elif damage == "foreign_id":
            records[0]["context"]["trace_id"] = "f" * 32
        elif damage == "duplicate":
            records.append(records[0])
        elif damage == "unfinished_child":
            records[0]["status"] = "unset"
        path.write_text(
            "\n".join(json.dumps(record) for record in records)
            + ("\nnot-json" if damage == "corrupt" else "\n")
        )
    summary = summarize_task(previously_observed, trace_store=TraceStore(trace_root))
    assert (
        summary.model_attempts
        is summary.tool_attempts
        is summary.permission_denials
        is None
    )
    assert summary.trace_diagnostic in ("unavailable", "incomplete")
    assert result.status is summary.status is TaskStatus.COMPLETED


def test_recovered_tool_failure_does_not_explain_a_later_callback_failure(
    tmp_path: Path,
) -> None:
    runner = make_runner(
        tmp_path,
        SequenceLLM(
            [
                LLMResponse(
                    tool_calls=[
                        ToolCall(id="invalid", name="missing-tool", arguments={})
                    ]
                )
            ]
        ),
        tracer=Tracer(JsonlTraceSink(tmp_path / "traces")),
    )

    def on_event(event: Any) -> None:
        if event.type == "agent_step" and event.data["step"] == 2:
            raise RuntimeError("host callback failed")

    runner.event_handler = on_event
    result = asyncio.run(runner.run(SPEC))
    summary = summarize_task(
        require_summary(runner), trace_store=TraceStore(tmp_path / "traces")
    )
    assert result.status is TaskStatus.RUNTIME_ERROR
    assert result.error is not None and "host callback failed" in result.error
    assert summary.failure_category == "runtime_error"
    assert summary.tool_failures == 1 and summary.model_failures == 0
    assert "host callback failed" not in summary.to_json()


def test_sensitive_content_and_long_ids_never_enter_the_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAIRN_ISSUE48_SECRET", SECRET)
    runner = make_runner(
        tmp_path,
        SequenceLLM(
            [
                LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id=SECRET,
                            name="read_file",
                            arguments={"path": "private.txt"},
                        )
                    ]
                ),
                LLMResponse(content=SECRET),
            ]
        ),
        tracer=Tracer(JsonlTraceSink(tmp_path / "traces")),
    )
    runner.secret_env_keys = frozenset({"CAIRN_ISSUE48_SECRET"})
    (runner.workspace.root / "private.txt").write_text(SECRET)
    result = asyncio.run(runner.run(TaskSpec(task_id=SECRET, prompt=SECRET)))
    summary = summarize_task(
        require_summary(runner),
        trace_store=TraceStore(tmp_path / "traces"),
        checks=[
            CheckResult(
                name=SECRET,
                passed=False,
                message=SECRET,
                error=SECRET,
                target_path=SECRET,
            )
        ],
    )
    text = summary.to_json()
    assert result.final_response == SECRET
    assert (
        SECRET not in text
        and "private.txt" not in text
        and "CAIRN_ISSUE48_SECRET" not in text
    )
    assert summary.task_id_hashed and summary.task_id.startswith("sha256:")
    runner.llm = SequenceLLM([LLMResponse(content="done")])
    asyncio.run(runner.run(TaskSpec(task_id="untrusted\n" * 10000, prompt="same")))
    assert require_summary(runner).task_id_hashed


def test_reused_runner_keeps_detached_task_snapshots(tmp_path: Path) -> None:
    runner = make_runner(
        tmp_path,
        SequenceLLM(
            [
                LLMResponse(
                    content="one", usage=LLMUsage(input_tokens=5, output_tokens=1)
                ),
                LLMResponse(content="two"),
            ]
        ),
        tracer=Tracer(JsonlTraceSink(tmp_path / "traces")),
    )
    asyncio.run(runner.run(SPEC))
    first = require_summary(runner)
    asyncio.run(runner.run(TaskSpec(task_id="second-task", prompt="other")))
    second = require_summary(runner)
    assert first.task_id == SPEC.task_id and second.task_id == "second-task"
    assert first.trace_id != second.trace_id
    assert first.provider_input_tokens == 5 and second.provider_input_tokens is None


def test_durable_identities_come_from_existing_session_events() -> None:
    with TemporaryDirectory(prefix="cairn-issue48-", dir=Path.home()) as directory:
        host = make_host(Path(directory))
        try:
            result = asyncio.run(host.runner.run(SPEC, durable_session=host.session))
            summary = require_summary(host.runner)
            last = host.session.last_turn
            assert last is not None
            assert summary.session_id == host.session.session_id
            assert summary.turn_id == last.turn_id
            assert summary.status is result.status is TaskStatus.COMPLETED
            assert summary.verification_verdict is summary.trace_id is None
            assert host.session.inspect().receipt == host.session.receipt
        finally:
            host.session.close()
