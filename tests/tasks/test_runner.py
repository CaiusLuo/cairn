import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cairn.assembly import build_agent
from cairn.core.agent import Agent
from cairn.core.budget import RunBudget
from cairn.core.context import ContextBudget, ContextBuilder
from cairn.core.events import Event
from cairn.core.loop import run_turn
from cairn.core.models import LLMResponse, Message, ToolCall
from cairn.core.permissions import PermissionResult
from cairn.llm.model_executor import ModelExecutor
from cairn.observability.tracer import Tracer
from cairn.repository import RepoContextProvider
from cairn.tasks import CodingTaskRunner, TaskResult, TaskSpec, TaskStatus
from cairn.tasks import runner as runner_module
from cairn.workspace.workspace import Workspace
from tests.support.runtime import FailingLLM, RecordingSink, SequenceLLM
from tests.test_repository import _git, _initialize_repository

SPEC = TaskSpec(task_id="task-18", name="local edit", prompt="Update tracked.txt")


def edit(path: str, old: str, new: str) -> LLMResponse:
    return LLMResponse(
        tool_calls=[
            ToolCall(
                id=f"edit-{path}",
                name="edit_file",
                arguments={"path": path, "old_text": old, "new_text": new},
            )
        ]
    )


def test_coding_task_uses_real_loop_and_preserves_runtime_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracked = _initialize_repository(tmp_path)
    revision = _git(tmp_path, "rev-parse", "HEAD")
    original_refs = _git(tmp_path, "show-ref")
    original_worktrees = _git(tmp_path, "worktree", "list", "--porcelain")
    workspace = Workspace(tmp_path)
    llm = SequenceLLM(
        [
            edit(tracked.name, "before\n", "after\n"),
            edit("new.txt", "", "new\n"),
            LLMResponse(content="Done (a model claim, not a verdict)"),
        ]
    )
    events: list[Event] = []
    sink = RecordingSink()
    tracer = Tracer(sink)
    builder = ContextBuilder()
    executor = ModelExecutor()
    agents: list[Agent] = []

    def handle_event(event: Event) -> None:
        events.append(event)

    def capture_agent(**kwargs: Any) -> Agent:
        agent = build_agent(**kwargs)
        agents.append(agent)
        return agent

    def permission_handler(tool_call: ToolCall) -> PermissionResult:
        pytest.fail("Workspace edits should use the existing baseline policy")

    loop = AsyncMock(wraps=run_turn)
    monkeypatch.setattr(runner_module, "build_agent", capture_agent)
    monkeypatch.setattr(runner_module, "run_turn", loop)
    runner = CodingTaskRunner(
        workspace=workspace,
        llm=llm,
        budget=RunBudget(max_steps=3),
        context_builder=builder,
        model_executor=executor,
        permission_handler=permission_handler,
        event_handler=handle_event,
        tracer=tracer,
        secret_env_keys=frozenset({"CUSTOM_PROVIDER_SECRET"}),
    )
    result = asyncio.run(runner.run(SPEC))

    assert result.task_id == SPEC.task_id
    assert result.status is TaskStatus.COMPLETED
    assert result.final_response == "Done (a model claim, not a verdict)"
    assert result.error is None
    assert result.repository.is_git_repository is True
    assert result.repository.branch == "main"
    assert result.repository.head_revision == revision
    assert result.repository.repository_root == workspace.root
    assert result.repository.workspace_root == workspace.root
    assert result.repository.dirty is True
    assert result.repository.changed_files == ("tracked.txt",)
    assert result.repository.untracked_files == ("new.txt",)
    assert result.repository.inspection_error is None
    assert tracked.read_text() == "after\n"
    assert (tmp_path / "new.txt").read_text() == "new\n"
    assert _git(tmp_path, "show-ref") == original_refs
    assert _git(tmp_path, "worktree", "list", "--porcelain") == original_worktrees
    assert workspace.root.is_dir()
    assert runner.workspace is workspace
    assert len(agents) == 1
    agent = agents[0]
    assert agent.llm is llm
    assert agent.context_builder is builder
    assert agent.model_executor is executor
    assert agent.permission_handler is permission_handler
    assert agent.tracer is tracer
    assert set(agent.tools.tools) == {"bash", "read_file", "edit_file"}
    assert len(llm.calls) == 3
    assert all(message.role != "system" for message in agent.state.messages)
    loop.assert_awaited_once_with(agent, SPEC.prompt, budget=runner.budget)
    assert events[0].type == "trace_start"
    assert events[-1].type == "trace_finish"
    assert result.trace_id == events[0].data["trace_id"]
    assert result.trace_id == events[-1].data["trace_id"]
    assert all(span.context.trace_id == result.trace_id for span in sink.spans)
    assert any(event.type == "tool_result" for event in events)
    assert TaskResult.model_validate_json(result.model_dump_json()) == result


def test_non_git_is_completed_without_claiming_verification(tmp_path: Path) -> None:
    runner = CodingTaskRunner(
        workspace=Workspace(tmp_path),
        llm=SequenceLLM([LLMResponse(content="I claim the tests pass")]),
        budget=RunBudget(max_steps=1),
    )
    result = asyncio.run(runner.run(SPEC))
    assert result.status is TaskStatus.COMPLETED
    assert result.trace_id is None
    assert result.repository.is_git_repository is False
    assert result.repository.inspection_error is None
    assert result.repository.head_revision is None
    assert result.repository.dirty is None
    assert list(tmp_path.iterdir()) == []


def test_step_budget_preserves_completed_tool_changes(tmp_path: Path) -> None:
    llm = SequenceLLM([edit("new.txt", "", "new\n")])
    runner = CodingTaskRunner(
        workspace=Workspace(tmp_path), llm=llm, budget=RunBudget(max_steps=1)
    )
    result = asyncio.run(runner.run(SPEC))
    assert result.status is TaskStatus.BUDGET_EXHAUSTED
    assert result.final_response is None
    assert result.error is not None and "RunBudgetExceeded" in result.error
    assert (tmp_path / "new.txt").read_text() == "new\n"
    assert len(llm.calls) == 1


def test_context_budget_maps_to_budget_exhaustion(tmp_path: Path) -> None:
    llm = SequenceLLM([])
    runner = CodingTaskRunner(
        workspace=Workspace(tmp_path),
        llm=llm,
        budget=RunBudget(max_steps=1),
        context_budget=ContextBudget(max_tokens=2, response_tokens=1),
    )
    result = asyncio.run(runner.run(SPEC))
    assert result.status is TaskStatus.BUDGET_EXHAUSTED
    assert result.error is not None and "ContextBudgetExceeded" in result.error
    assert llm.calls == []


def test_runtime_exception_and_trace_are_reported(tmp_path: Path) -> None:
    events: list[Event] = []

    def handle_event(event: Event) -> None:
        events.append(event)

    runner = CodingTaskRunner(
        workspace=Workspace(tmp_path),
        llm=FailingLLM(),
        budget=RunBudget(max_steps=1),
        event_handler=handle_event,
        tracer=Tracer(RecordingSink()),
    )
    result = asyncio.run(runner.run(SPEC))
    assert result.status is TaskStatus.RUNTIME_ERROR
    assert result.error == "RuntimeError: llm failed"
    assert result.final_response is None
    assert result.trace_id == events[-1].data["trace_id"]
    assert events[-1].data["status"] == "error"
    assert tmp_path.is_dir()


def test_cancellation_before_execution_skips_agent_and_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        cancellation = asyncio.Event()
        cancellation.set()
        llm = SequenceLLM([])
        build = AsyncMock()
        monkeypatch.setattr(runner_module, "build_agent", build)
        result = await CodingTaskRunner(
            workspace=Workspace(tmp_path), llm=llm, budget=RunBudget(max_steps=1)
        ).run(SPEC, cancellation_event=cancellation)
        assert result.status is TaskStatus.CANCELLED
        assert result.trace_id is None
        assert result.final_response is None
        assert result.repository.is_git_repository is False
        assert llm.calls == []
        build.assert_not_called()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("external", "watch_cancellation"), [(False, True), (True, False), (True, True)]
)
def test_cancellation_waits_for_children_and_preserves_trace(
    tmp_path: Path, external: bool, watch_cancellation: bool
) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        cleaning = asyncio.Event()
        finish_cleanup = asyncio.Event()
        settled = asyncio.Event()
        cancellation = asyncio.Event()
        events: list[Event] = []
        sink = RecordingSink()

        def handle_event(event: Event) -> None:
            events.append(event)

        class SlowLLM:
            async def generate(
                self,
                messages: list[Message],
                tools: list[dict[str, Any]] | None = None,
            ) -> LLMResponse:
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    await finish_cleanup.wait()
                    settled.set()
                return LLMResponse(content="unreachable")

        runner = CodingTaskRunner(
            workspace=Workspace(tmp_path),
            llm=SlowLLM(),
            budget=RunBudget(max_steps=1),
            event_handler=handle_event,
            tracer=Tracer(sink),
        )
        baseline = asyncio.all_tasks()
        task = asyncio.create_task(
            runner.run(
                SPEC,
                cancellation_event=cancellation if watch_cancellation else None,
            )
        )
        await asyncio.wait_for(started.wait(), 3)
        if external:
            task.cancel("caller cancellation")
        else:
            cancellation.set()
        await asyncio.wait_for(cleaning.wait(), 3)
        assert not task.done()
        if external:
            task.cancel("second caller cancellation")
        finish_cleanup.set()
        if external:
            with pytest.raises(asyncio.CancelledError, match="caller cancellation"):
                await task
        else:
            result = await task
            assert result.status is TaskStatus.CANCELLED
            assert result.repository.is_git_repository is False
            assert result.trace_id == events[0].data["trace_id"]
        assert settled.is_set()
        assert events[-1].type == "trace_finish"
        assert events[-1].data["status"] == "error"
        assert sink.spans[-1].error == "CancelledError: turn cancelled"
        assert asyncio.all_tasks() == baseline
        assert tmp_path.is_dir()

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_external_cancellation_during_cooperative_cleanup_is_not_swallowed(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        cleaning = asyncio.Event()
        finish = asyncio.Event()
        cancellation = asyncio.Event()
        settled = asyncio.Event()

        class SlowLLM:
            async def generate(
                self,
                messages: list[Message],
                tools: list[dict[str, Any]] | None = None,
            ) -> LLMResponse:
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    await finish.wait()
                    settled.set()
                return LLMResponse(content="unreachable")

        runner = CodingTaskRunner(
            workspace=Workspace(tmp_path), llm=SlowLLM(), budget=RunBudget(max_steps=1)
        )
        task = asyncio.create_task(runner.run(SPEC, cancellation_event=cancellation))
        await started.wait()
        cancellation.set()
        await cleaning.wait()
        task.cancel("external during cleanup")
        await asyncio.sleep(0)
        assert not settled.is_set()
        finish.set()
        with pytest.raises(asyncio.CancelledError, match="external during cleanup"):
            await task
        assert settled.is_set()

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_inspection_failure_preserves_final_response_and_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        RepoContextProvider,
        "inspect_evidence",
        AsyncMock(side_effect=OSError("inspection unavailable")),
    )
    runner = CodingTaskRunner(
        workspace=Workspace(tmp_path),
        llm=SequenceLLM([LLMResponse(content="done")]),
        budget=RunBudget(max_steps=1),
        tracer=Tracer(RecordingSink()),
    )
    result = asyncio.run(runner.run(SPEC))
    assert result.status is TaskStatus.RUNTIME_ERROR
    assert result.final_response == "done"
    assert result.trace_id is not None
    assert result.repository.is_git_repository is None
    assert result.repository.inspection_error == "OSError: inspection unavailable"
    assert (
        result.error == "Repository inspection failed: OSError: inspection unavailable"
    )


def test_corrupt_git_configuration_is_not_a_non_git_success(tmp_path: Path) -> None:
    _initialize_repository(tmp_path)
    (tmp_path / ".git" / "config").write_text("[broken configuration\n")
    runner = CodingTaskRunner(
        workspace=Workspace(tmp_path),
        llm=SequenceLLM([LLMResponse(content="done")]),
        budget=RunBudget(max_steps=1),
    )
    result = asyncio.run(runner.run(SPEC))
    assert result.status is TaskStatus.RUNTIME_ERROR
    assert result.final_response == "done"
    assert result.repository.is_git_repository is None
    assert result.repository.inspection_error is not None
    assert "repository discovery" in result.repository.inspection_error


def test_execution_error_survives_inspection_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        RepoContextProvider,
        "inspect_evidence",
        AsyncMock(side_effect=OSError("inspection unavailable")),
    )
    result = asyncio.run(
        CodingTaskRunner(
            workspace=Workspace(tmp_path),
            llm=FailingLLM(),
            budget=RunBudget(max_steps=1),
        ).run(SPEC)
    )
    assert result.status is TaskStatus.RUNTIME_ERROR
    assert result.error is not None
    assert result.error.startswith("RuntimeError: llm failed;")
    assert "inspection unavailable" in result.error


def test_fresh_agent_per_task_preserves_previously_returned_result(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        llm = SequenceLLM([LLMResponse(content="first"), LLMResponse(content="second")])
        runner = CodingTaskRunner(
            workspace=Workspace(tmp_path), llm=llm, budget=RunBudget(max_steps=1)
        )
        first = await runner.run(SPEC)
        second = await runner.run(TaskSpec(task_id="second", prompt="another mission"))
        assert first.task_id == SPEC.task_id and first.final_response == "first"
        assert second.task_id == "second" and second.final_response == "second"
        assert [
            message.role for message in llm.calls[1][0] if message.role != "system"
        ] == ["user"]
        assert tmp_path.is_dir()

    asyncio.run(scenario())


def test_ambiguous_context_injection_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not both"):
        CodingTaskRunner(
            workspace=Workspace(tmp_path),
            llm=SequenceLLM([]),
            budget=RunBudget(max_steps=1),
            context_builder=ContextBuilder(),
            context_budget=ContextBudget(),
        )


def test_external_cancellation_during_inspection_settles_before_propagating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        cleaning = asyncio.Event()
        finish = asyncio.Event()
        settled = asyncio.Event()

        async def inspect(
            self: RepoContextProvider, *, secret_env_keys: frozenset[str] = frozenset()
        ) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await finish.wait()
                settled.set()

        monkeypatch.setattr(RepoContextProvider, "inspect_evidence", inspect)
        runner = CodingTaskRunner(
            workspace=Workspace(tmp_path),
            llm=SequenceLLM([LLMResponse(content="done")]),
            budget=RunBudget(max_steps=1),
        )
        baseline = asyncio.all_tasks()
        task = asyncio.create_task(runner.run(SPEC))
        await started.wait()
        task.cancel("inspection interrupted")
        await cleaning.wait()
        task.cancel("repeated inspection cancellation")
        assert not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError, match="inspection interrupted"):
            await task
        assert settled.is_set()
        assert asyncio.all_tasks() == baseline

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_injected_cancelled_error_is_not_a_runtime_error(tmp_path: Path) -> None:
    class CancelledLLM:
        async def generate(
            self, messages: list[Message], tools: list[dict[str, Any]] | None = None
        ) -> LLMResponse:
            raise asyncio.CancelledError("injected cancellation")

    async def scenario() -> None:
        runner = CodingTaskRunner(
            workspace=Workspace(tmp_path),
            llm=CancelledLLM(),
            budget=RunBudget(max_steps=1),
        )
        with pytest.raises(asyncio.CancelledError, match="injected cancellation"):
            await runner.run(SPEC, cancellation_event=asyncio.Event())

    asyncio.run(scenario())


def test_event_forwarding_failure_preserves_finished_response(tmp_path: Path) -> None:
    def handler(event: Event) -> None:
        if event.type == "trace_finish":
            raise RuntimeError("event sink failed")

    result = asyncio.run(
        CodingTaskRunner(
            workspace=Workspace(tmp_path),
            llm=SequenceLLM([LLMResponse(content="done")]),
            budget=RunBudget(max_steps=1),
            event_handler=handler,
            tracer=Tracer(RecordingSink()),
        ).run(SPEC)
    )
    assert result.status is TaskStatus.RUNTIME_ERROR
    assert result.final_response == "done"
    assert result.error == "RuntimeError: event sink failed"
    assert result.trace_id is not None


@pytest.mark.parametrize("external", [False, True])
def test_cancel_cleanup_failure_stays_observable(
    tmp_path: Path, external: bool
) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        cancellation = asyncio.Event()

        class CleanupFailureLLM:
            async def generate(
                self, messages: list[Message], tools: list[dict[str, Any]] | None = None
            ) -> LLMResponse:
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    raise RuntimeError("LLM cleanup failed")

        runner = CodingTaskRunner(
            workspace=Workspace(tmp_path),
            llm=CleanupFailureLLM(),
            budget=RunBudget(max_steps=1),
        )
        task = asyncio.create_task(runner.run(SPEC, cancellation_event=cancellation))
        await started.wait()
        if external:
            task.cancel("cancel with cleanup failure")
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
            assert "LLM cleanup failed" in " ".join(caught.value.__notes__)
        else:
            cancellation.set()
            result = await task
            assert result.status is TaskStatus.RUNTIME_ERROR
            assert result.error == "RuntimeError: LLM cleanup failed"

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_unused_cancellation_watcher_settles_on_success(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner = CodingTaskRunner(
            workspace=Workspace(tmp_path),
            llm=SequenceLLM([LLMResponse(content="done")]),
            budget=RunBudget(max_steps=1),
        )
        baseline = asyncio.all_tasks()
        result = await runner.run(SPEC, cancellation_event=asyncio.Event())
        assert result.status is TaskStatus.COMPLETED
        assert result.final_response == "done"
        assert asyncio.all_tasks() == baseline

    asyncio.run(scenario())
