import asyncio
import builtins
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cairn.assembly import build_agent as real_build_agent
from cairn.core.budget import RunBudget
from cairn.core.context import ContextBudget, ContextBuilder
from cairn.core.models import LLMResponse, ToolCall
from cairn.evals import (
    CheckResult,
    EvalCase,
    EvalRunner,
    EvalStatus,
    FileContentEqualsCheck,
    FileExistsCheck,
)
from cairn.evals import runner as runner_module
from cairn.llm.base import LLMClient
from cairn.observability.sinks import JsonlTraceSink
from cairn.observability.tracer import Tracer
from cairn.tools.bash import BashTool
from cairn.workspace.workspace import Workspace
from tests.support.runtime import RecordingSink, SequenceLLM


def _edit_response(
    path: str, old_text: str, new_text: str, *, call_id: str = "edit-1"
) -> LLMResponse:
    return LLMResponse(
        tool_calls=[
            ToolCall(
                id=call_id,
                name="edit_file",
                arguments={"path": path, "old_text": old_text, "new_text": new_text},
            )
        ]
    )


def _network_response() -> LLMResponse:
    return LLMResponse(
        tool_calls=[
            ToolCall(
                id="network-1",
                name="bash",
                arguments={
                    "command": "touch network-side-effect",
                    "network_access": True,
                    "justification": "request an external resource",
                },
            )
        ]
    )


def _runner(
    factory: Callable[[], LLMClient],
    *,
    budget: RunBudget | None = None,
    temp_root: Path | None = None,
    tracer: Tracer | None = None,
) -> EvalRunner:
    return EvalRunner(
        factory,
        budget=budget or RunBudget(max_steps=20),
        run_timeout_seconds=5,
        check_timeout_seconds=5,
        temp_root=temp_root,
        tracer=tracer,
    )


class NamedCheck:
    def __init__(self, name: str, evaluate: Callable[[Workspace], Any]) -> None:
        self.name = name
        self._evaluate = evaluate

    async def evaluate(self, workspace: Workspace) -> Any:
        return await self._evaluate(workspace)


def test_model_claim_does_not_pass_when_workspace_is_unchanged() -> None:
    llm = SequenceLLM([LLMResponse(content="Done. I changed the file.")])
    result = asyncio.run(
        _runner(lambda: llm).run(
            EvalCase(
                name="unchanged",
                prompt="Change answer.txt from old to expected.",
                files={"answer.txt": "old"},
            ),
            checks=[FileContentEqualsCheck("answer.txt", "expected")],
        )
    )

    assert result.status is EvalStatus.FAIL
    assert result.error is None
    assert result.checks[0].passed is False
    assert llm.calls


def test_real_edit_file_call_passes_content_check() -> None:
    llm = SequenceLLM(
        [
            _edit_response("docs/例/答え.txt", "こんにちは\n", "こんにちは 🌱\n"),
            LLMResponse(content="Done."),
        ]
    )
    result = asyncio.run(
        _runner(lambda: llm).run(
            EvalCase(
                name="edit",
                prompt="Update the nested answer file.",
                files={"docs/例/答え.txt": "こんにちは\n"},
            ),
            checks=[FileContentEqualsCheck("docs/例/答え.txt", "こんにちは 🌱\n")],
        )
    )

    assert result.status is EvalStatus.PASS
    assert result.checks[0].passed is True


def test_runs_isolate_fixtures_history_and_workspace(tmp_path: Path) -> None:
    parent = tmp_path / "cases"
    parent.mkdir()
    sentinel = parent / "caller-owned.txt"
    sentinel.write_text("keep", encoding="utf-8")
    first_llm = SequenceLLM(
        [
            _edit_response("same.txt", "x", "changed by case one"),
            _edit_response("first-only.txt", "", "private first history"),
            LLMResponse(content="case one done"),
        ]
    )
    second_llm = SequenceLLM(
        [
            _edit_response("same.txt", "x", "changed by case two"),
            LLMResponse(content="case two done"),
        ]
    )
    instances = iter([first_llm, second_llm])
    workspaces: list[Workspace] = []
    runner = _runner(lambda: next(instances), temp_root=parent)
    first_prompt = "case one: change the fixture"
    second_prompt = "case two: inspect only the original fixture"

    async def check_first(workspace: Workspace) -> CheckResult:
        workspaces.append(workspace)
        changed = workspace.resolve_path("same.txt").read_text(encoding="utf-8")
        first_only = workspace.resolve_path("first-only.txt").read_text(
            encoding="utf-8"
        )
        return CheckResult(
            name="record_workspace",
            passed=changed == "changed by case one"
            and first_only == "private first history",
        )

    async def check_second(workspace: Workspace) -> CheckResult:
        workspaces.append(workspace)
        changed = workspace.resolve_path("same.txt").read_text(encoding="utf-8")
        first_only = workspace.resolve_path("first-only.txt")
        return CheckResult(
            name="record_workspace",
            passed=changed == "changed by case two" and not first_only.exists(),
        )

    first = asyncio.run(
        runner.run(
            EvalCase(
                name="same-name",
                prompt=first_prompt,
                files={"same.txt": "x"},
            ),
            checks=[NamedCheck("record_workspace", check_first)],
        )
    )
    second = asyncio.run(
        runner.run(
            EvalCase(
                name="same-name",
                prompt=second_prompt,
                files={"same.txt": "x"},
            ),
            checks=[NamedCheck("record_workspace", check_second)],
        )
    )

    assert first.status is EvalStatus.PASS
    assert second.status is EvalStatus.PASS
    assert first_llm is not second_llm
    assert len(first_llm.calls) == 3
    assert len(second_llm.calls) == 2
    second_messages = second_llm.calls[0][0]
    assert second_messages[-1].content == second_prompt
    assert all(message.role != "tool" for message in second_messages)
    assert all(message.role != "assistant" for message in second_messages)
    assert [
        message.content for message in second_messages if message.role == "user"
    ] == [second_prompt]
    assert len(workspaces) == 2
    assert workspaces[0] is not workspaces[1]
    assert workspaces[0].root != workspaces[1].root
    assert all(not workspace.root.exists() for workspace in workspaces)
    assert parent.is_dir()
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert list(parent.iterdir()) == [sentinel]


@pytest.mark.parametrize("unsafe_kind", ["parent", "absolute", "git"])
def test_unsafe_fixture_path_errors_before_factory_and_outside_write(
    unsafe_kind: str, tmp_path: Path
) -> None:
    case_parent = tmp_path / "cases"
    case_parent.mkdir()
    traversal_target = case_parent / "outside-parent.txt"
    traversal_target.write_text("caller-owned traversal sentinel", encoding="utf-8")
    absolute_target = tmp_path / "outside-absolute.txt"
    absolute_target.write_text("caller-owned absolute sentinel", encoding="utf-8")
    unsafe_path = {
        "parent": "../outside-parent.txt",
        "absolute": str(absolute_target),
        "git": ".git/config",
    }[unsafe_kind]
    calls = 0

    def factory() -> LLMClient:
        nonlocal calls
        calls += 1
        return SequenceLLM([LLMResponse(content="Done")])

    result = asyncio.run(
        _runner(factory, temp_root=case_parent).run(
            EvalCase(name="unsafe", prompt="Inspect", files={unsafe_path: "bad"}),
            checks=[FileExistsCheck("safe.txt")],
        )
    )

    assert result.status is EvalStatus.ERROR
    assert result.error is not None
    assert result.checks == []
    assert calls == 0
    assert (
        traversal_target.read_text(encoding="utf-8")
        == "caller-owned traversal sentinel"
    )
    assert (
        absolute_target.read_text(encoding="utf-8") == "caller-owned absolute sentinel"
    )
    assert list(case_parent.iterdir()) == [traversal_target]


def test_empty_checks_error_without_factory_call() -> None:
    calls = 0

    def factory() -> LLMClient:
        nonlocal calls
        calls += 1
        raise AssertionError("factory must not run")

    result = asyncio.run(
        _runner(factory).run(EvalCase(name="empty", prompt="Do nothing"), checks=[])
    )

    assert result.status is EvalStatus.ERROR
    assert result.error is not None
    assert calls == 0


@pytest.mark.parametrize(
    "check",
    [FileExistsCheck("../outside"), FileContentEqualsCheck("/tmp/outside", "")],
)
def test_malformed_check_path_becomes_check_error(check: Any) -> None:
    llm = SequenceLLM([LLMResponse(content="done")])
    result = asyncio.run(
        _runner(lambda: llm).run(
            EvalCase(name="bad-check-path", prompt="Inspect"), checks=[check]
        )
    )

    assert result.status is EvalStatus.ERROR
    assert result.error is None
    assert result.checks[0].error is not None
    assert result.checks[0].passed is False


@pytest.mark.parametrize("raise_check", [False, True])
def test_failed_check_continues_and_error_takes_precedence(
    raise_check: bool,
) -> None:
    llm = SequenceLLM([LLMResponse(content="done")])
    later: list[bool] = []

    async def raises(workspace: Workspace) -> CheckResult:
        raise RuntimeError("checker broke")

    async def passing(workspace: Workspace) -> CheckResult:
        later.append(True)
        return CheckResult(name="later", passed=True)

    checks = [NamedCheck("ordinary_failure", _ordinary_failure)]
    if raise_check:
        checks.append(NamedCheck("raises", raises))
    checks.append(NamedCheck("later", passing))

    result = asyncio.run(
        _runner(lambda: llm).run(
            EvalCase(name="mixed", prompt="Inspect"),
            checks=checks,
        )
    )

    assert result.status is (EvalStatus.ERROR if raise_check else EvalStatus.FAIL)
    assert [item.passed for item in result.checks] == (
        [False, False, True] if raise_check else [False, True]
    )
    assert result.checks[0].error is None
    if raise_check:
        assert result.checks[1].error == "RuntimeError: checker broke"
    assert later == [True]


async def _ordinary_failure(workspace: Workspace) -> CheckResult:
    return CheckResult(name="ordinary_failure", passed=False, message="not satisfied")


@pytest.mark.parametrize(
    ("name", "returned", "expected_error"),
    [
        ("", CheckResult(name="unused", passed=True), "ValueError"),
        ("valid", object(), "TypeError"),
    ],
)
def test_invalid_check_name_or_return_is_error(
    name: str, returned: Any, expected_error: str
) -> None:
    llm = SequenceLLM([LLMResponse(content="done")])

    async def evaluate(workspace: Workspace) -> Any:
        return returned

    result = asyncio.run(
        _runner(lambda: llm).run(
            EvalCase(name="malformed-check", prompt="Inspect"),
            checks=[NamedCheck(name, evaluate)],
        )
    )

    assert result.status is EvalStatus.ERROR
    assert result.checks[0].error is not None
    assert expected_error in result.checks[0].error


@pytest.mark.parametrize("failure", ["budget", "llm"])
def test_execution_error_after_edit_keeps_final_state_evidence(
    failure: str,
) -> None:
    llm = SequenceLLM([_edit_response("answer.txt", "old", "new")])
    budget = RunBudget(max_steps=1) if failure == "budget" else RunBudget(max_steps=20)
    expected_error = "RunBudgetExceeded" if failure == "budget" else "IndexError"
    result = asyncio.run(
        _runner(lambda: llm, budget=budget).run(
            EvalCase(
                name=f"{failure}-after-edit",
                prompt="Edit the answer",
                files={"answer.txt": "old"},
            ),
            checks=[FileContentEqualsCheck("answer.txt", "new")],
        )
    )

    assert result.status is EvalStatus.ERROR
    assert result.error is not None
    assert expected_error in result.error
    assert result.checks[0].passed is True


@pytest.mark.parametrize("failure", ["factory", "build"])
def test_setup_failure_records_error_and_runs_safe_checks(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def factory() -> LLMClient:
        calls.append("factory")
        if failure == "factory":
            raise RuntimeError("factory failed")
        return SequenceLLM([LLMResponse(content="unused")])

    if failure == "build":

        def fail_build(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("build failed")

        monkeypatch.setattr(runner_module, "build_agent", fail_build)

    checked: list[str] = []

    async def check(workspace: Workspace) -> CheckResult:
        checked.append("checked")
        return CheckResult(name="safe", passed=True)

    result = asyncio.run(
        _runner(factory).run(
            EvalCase(name=failure, prompt="unused", files={"safe.txt": "ok"}),
            checks=[NamedCheck("safe", check)],
        )
    )

    assert result.status is EvalStatus.ERROR
    assert result.error is not None
    assert f"{failure} failed" in result.error
    assert checked == ["checked"]
    assert calls == ["factory"]


def test_trace_uses_external_sink_and_no_trace_id_without_tracer(
    tmp_path: Path,
) -> None:
    trace_dir = tmp_path / "trace-sink"
    sink = JsonlTraceSink(trace_dir)
    llm = SequenceLLM([LLMResponse(content="done")])
    case_root = tmp_path / "cases"
    case_root.mkdir()
    result = asyncio.run(
        _runner(lambda: llm, temp_root=case_root, tracer=Tracer(sink)).run(
            EvalCase(name="traced", prompt="Inspect"),
            checks=[FileExistsCheck("missing.txt")],
        )
    )

    assert result.trace_id is not None
    trace_files = list(trace_dir.glob("*.jsonl"))
    assert len(trace_files) == 1
    assert trace_files[0].stem == result.trace_id
    assert trace_files[0].is_file()
    assert trace_dir != case_root
    assert list(case_root.iterdir()) == []

    untraced = asyncio.run(
        _runner(lambda: SequenceLLM([LLMResponse(content="done")])).run(
            EvalCase(name="untraced", prompt="Inspect"),
            checks=[FileExistsCheck("missing.txt")],
        )
    )
    assert untraced.trace_id is None


def test_network_permission_request_fails_closed_without_prompt_or_execution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sink = RecordingSink()
    llm = SequenceLLM(
        [_network_response(), LLMResponse(content="Could not access it.")]
    )
    prompt_calls: list[bool] = []
    execute_calls: list[bool] = []

    def forbidden_input(*args: Any, **kwargs: Any) -> str:
        prompt_calls.append(True)
        raise AssertionError("eval must not prompt for permission")

    async def forbidden_execute(*args: Any, **kwargs: Any) -> Any:
        execute_calls.append(True)
        raise AssertionError("network Bash execution must not occur")

    def forbidden_ui_prompt(*args: Any, **kwargs: Any) -> Any:
        prompt_calls.append(True)
        raise AssertionError("eval must not show a UI permission prompt")

    monkeypatch.setattr(builtins, "input", forbidden_input)
    monkeypatch.setattr("cairn.ui.console_permission_prompt", forbidden_ui_prompt)
    monkeypatch.setattr(BashTool, "execute", forbidden_execute)
    case_root = tmp_path / "cases"
    case_root.mkdir()
    result = asyncio.run(
        _runner(lambda: llm, temp_root=case_root, tracer=Tracer(sink)).run(
            EvalCase(name="network", prompt="Fetch something"),
            checks=[FileExistsCheck("network-side-effect")],
        )
    )

    assert result.status is EvalStatus.FAIL
    assert prompt_calls == []
    assert execute_calls == []
    assert not (tmp_path / "network-side-effect").exists()
    permission_span = next(
        span for span in sink.spans if span.name == "permission.check"
    )
    assert permission_span.attributes["source"] == "no_handler"
    assert permission_span.attributes["granted_capabilities"] == []
    tool_message = next(
        message for message in llm.calls[1][0] if message.role == "tool"
    )
    failure = json.loads(tool_message.content or "{}")
    assert failure["type"] == "PermissionRequired"
    assert "no permission handler" in failure["error"]
    assert list(case_root.iterdir()) == []


def _capturing_runner(
    monkeypatch: pytest.MonkeyPatch,
    llm: SequenceLLM,
    tmp_path: Path,
    captured: list[dict[str, Any]],
    *,
    context_budget: ContextBudget | None = None,
) -> EvalRunner:
    original = real_build_agent

    def capture(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(runner_module, "build_agent", capture)
    return EvalRunner(
        lambda: llm,
        budget=RunBudget(max_steps=2),
        run_timeout_seconds=5,
        check_timeout_seconds=5,
        context_budget=context_budget,
        temp_root=tmp_path,
    )


@pytest.mark.parametrize("configured", [True, False])
def test_runner_passes_an_explicit_request_context_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    configured: bool,
) -> None:
    captured: list[dict[str, Any]] = []
    llm = SequenceLLM([LLMResponse(content="done")])
    context_budget = ContextBudget(max_tokens=2048, response_tokens=256)
    runner = _capturing_runner(
        monkeypatch,
        llm,
        tmp_path,
        captured,
        context_budget=context_budget if configured else None,
    )

    result = asyncio.run(
        runner.run(
            EvalCase(name="budget", prompt="Say done.", files={"note.txt": "x"}),
            checks=[FileExistsCheck("note.txt")],
        )
    )

    assert result.status is EvalStatus.PASS
    builder = captured[0]["context_builder"]
    assert isinstance(builder, ContextBuilder)
    expected = context_budget if configured else ContextBudget()
    assert runner.context_budget == expected
    assert builder.budget is runner.context_budget
