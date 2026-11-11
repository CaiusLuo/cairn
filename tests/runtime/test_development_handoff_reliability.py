"""S2-Fix reliability tests for durable Development Handoff."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest

from cairn.agent.budget import RunBudget
from cairn.agent.models.messages import LLMResponse, LLMUsage, Message, ToolCall
from cairn.agent.models.offline import OfflineDevelopmentModel
from cairn.agent.session.models import CheckpointReceipt, Revision
from cairn.conversation.models import Conversation, ConversationSource, Run, RunStatus
from cairn.conversation.store import DomainStore
from cairn.development.catalogue import RuntimeCatalogue, RuntimeTask
from cairn.development.handoff import (
    DevelopmentHandoff,
    HandoffReviewConflict,
    HandoffReviewFailed,
)
from cairn.development.models import HandoffStatus, Task, TaskStatus
from cairn.development.service import TaskService
from cairn.server.bootstrap import runtime_app
from cairn.server.client import RuntimeClient
from tests.support.git import initialize_repository


class _NoopExecution:
    sensitive_values: tuple[str, ...] = ()

    async def __call__(self, *args: Any, **kwargs: Any) -> str:
        raise AssertionError("test should not execute an agent")


class _BadSessionStore:
    def inspect(self, session_id: str) -> SimpleNamespace:
        return SimpleNamespace(status="executing")


def _task_service(tmp_path: Path) -> tuple[Path, DomainStore, TaskService]:
    source = tmp_path / "source"
    source.mkdir()
    store = DomainStore(tmp_path / "domain.sqlite3")
    catalogue = RuntimeCatalogue(
        tmp_path / "runtime",
        forbidden_roots=(source, tmp_path / "worktrees"),
    )
    tasks = TaskService(catalogue, _NoopExecution())
    tasks.worker = asyncio.create_task(asyncio.sleep(3600))
    return source, store, tasks


def _run(store: DomainStore, source: Path, *, request_id: str = "handoff-run") -> Run:
    conversation = store.create_conversation(
        workspace=source,
        source=ConversationSource.CLI,
        initial_prompt="goal",
    )
    return store.create_run(
        conversation_id=conversation.conversation_id,
        status=RunStatus.RUNNING,
        prompt="go create",
        request_id=request_id,
        source=ConversationSource.CLI,
    )


async def _close_tasks(tasks: TaskService) -> None:
    worker = tasks.worker
    assert worker is not None
    worker.cancel()
    with suppress(asyncio.CancelledError):
        await worker
    tasks.catalogue.close()


class _HandoffModel(OfflineDevelopmentModel):
    _asked = False

    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        names = {tool["function"]["name"] for tool in tools or ()}
        if (
            "begin_development" in names
            and any(
                message.role == "user"
                and "Create the development artifact" in (message.content or "")
                for message in messages
            )
            and not type(self)._asked
        ):
            type(self)._asked = True
            return LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="begin-development",
                        name="begin_development",
                        arguments={"prompt": "Create the development artifact"},
                    )
                ],
                usage=LLMUsage(input_tokens=10, output_tokens=3),
            )
        return await super().generate(messages, tools)


def test_handoff_exception_keeps_conversation_worker_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        _HandoffModel._asked = False
        source = tmp_path / "source"
        source.mkdir()
        initialize_repository(source)
        app = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="zzzz",
        )
        app.state.execution.llm_factory = _HandoffModel
        async with app.router.lifespan_context(app):
            api = RuntimeClient(
                "http://127.0.0.1:8777",
                "zzzz",
                transport=httpx.ASGITransport(app=app),
            )
            conversation = await api.create_conversation("hello", source="cli")
            handoff = app.state.conversations.development_handoff
            original_complete = handoff.complete

            def explode(*args: Any, **kwargs: Any) -> None:
                raise RuntimeError("injected handoff failure")

            monkeypatch.setattr(handoff, "complete", explode)
            failed = await api.send_conversation(
                conversation["conversation_id"],
                "Create the development artifact",
                source="cli",
                request_id="handoff-failure",
            )
            for _ in range(250):
                current = await api.get_run(failed["run_id"])
                if current["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
            assert current["status"] == "recovery_required", current
            assert current["error"] == "development_handoff_failed"
            worker = app.state.conversations.worker
            assert worker is not None and not worker.done()

            monkeypatch.setattr(handoff, "complete", original_complete)
            next_conversation = await api.create_conversation("normal", source="cli")
            next_run = await api.send_conversation(
                next_conversation["conversation_id"],
                "just a normal message",
                source="cli",
                request_id="normal-after-failure",
            )
            for _ in range(250):
                current = await api.get_run(next_run["run_id"])
                if current["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
            assert current["status"] == "completed", current
            assert not app.state.conversations.worker.done()

    asyncio.run(scenario())


def test_task_service_records_history_origins_before_worker_can_take_task(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source = tmp_path / "source"
        source.mkdir()
        catalogue = RuntimeCatalogue(
            tmp_path / "runtime",
            forbidden_roots=(source, tmp_path / "worktrees"),
        )
        seen: list[tuple[CheckpointReceipt, ...]] = []

        async def executor(task: RuntimeTask, emit: Any) -> str:
            del emit
            seen.append(task.history_origins)
            return "ok"

        tasks = TaskService(catalogue, executor)
        await tasks.start()
        receipt = CheckpointReceipt(
            revision=Revision(session_id=str(uuid4()), version=0, head_seq=0),
            turn_id=None,
            sha256="a" * 64,
            outcome=None,
        )
        try:
            task = tasks.submit(
                "hello",
                "cli",
                request_id="history-before-admission",
                history_origins=(receipt,),
            )
            for _ in range(100):
                if tasks.inspect(task.task_id).status == "completed":
                    break
                await asyncio.sleep(0.01)
            assert seen == [(receipt,)]
            assert tasks.inspect(task.task_id).status == "completed"
        finally:
            await tasks.close()

    asyncio.run(scenario())


def test_unverifiable_checkpoint_enters_needs_review_without_task_execution(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source, store, tasks = _task_service(tmp_path)
        try:
            run = _run(store, source)
            execution = SimpleNamespace(store=_BadSessionStore())
            handoff = DevelopmentHandoff(store, execution, tasks)  # type: ignore[arg-type]
            with pytest.raises(ValueError, match="checkpoint_not_clean"):
                handoff.complete(
                    run,
                    RuntimeTask(
                        prompt="go create",
                        source="cli",
                        mode="development",
                        session_id=str(uuid4()),
                    ),
                    lambda: {"budget": {}},
                )
            record = store.get_handoff(run.run_id)
            assert record is not None
            assert record.status is HandoffStatus.NEEDS_REVIEW
            assert record.diagnostic == "history_origins_unavailable"
            assert tasks.list_tasks() == []
        finally:
            await _close_tasks(tasks)

    asyncio.run(scenario())


def _receipt(turn_id: str) -> CheckpointReceipt:
    return CheckpointReceipt(
        revision=Revision(
            session_id=str(uuid4()),
            version=1,
            head_seq=1,
        ),
        turn_id=turn_id,
        sha256="b" * 64,
        outcome="completed",
    )


def test_existing_runtime_task_with_incomplete_history_enters_needs_review(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source, store, tasks = _task_service(tmp_path)
        try:
            run = _run(store, source)
            turn_id = str(uuid4())
            receipt = _receipt(turn_id)
            receipt_report = SimpleNamespace(
                status="clean",
                revision=receipt.revision,
                receipt=receipt,
                attempts=(SimpleNamespace(intent=SimpleNamespace(turn_id=turn_id)),),
            )
            execution = SimpleNamespace(
                store=SimpleNamespace(inspect=lambda session_id: receipt_report)
            )
            handoff = DevelopmentHandoff(store, execution, tasks)  # type: ignore[arg-type]
            store.begin_handoff(
                run_id=run.run_id,
                conversation_id=run.conversation_id,
                request_id="handoff:" + run.run_id,
                prompt="go create",
                source="cli",
                history_origins=(receipt.model_dump(mode="json"),),
            )
            runtime = tasks.submit(
                "go create",
                "cli",
                request_id="handoff:" + run.run_id,
                mode="development",
                conversation_key=run.conversation_id,
            )
            store.update_handoff(
                run.run_id,
                status=HandoffStatus.RUNTIME_CREATED,
                runtime_task_id=runtime.task_id,
            )
            recovered = handoff.recover_incomplete()
            assert recovered == ()
            record = store.get_handoff(run.run_id)
            assert record is not None
            assert record.status is HandoffStatus.NEEDS_REVIEW
            assert record.diagnostic == "history_origins_mismatch"
        finally:
            await _close_tasks(tasks)

    asyncio.run(scenario())


def test_handoff_review_conflict_is_explicit() -> None:
    assert issubclass(HandoffReviewConflict, ValueError)


def _seed_handoff_review(
    app: Any, source: Path
) -> tuple[Conversation, Run, RuntimeTask, Task]:
    store = app.state.conversations.store
    conversation = store.create_conversation(
        workspace=source,
        source=ConversationSource.CLI,
        initial_prompt="review handoff",
    )
    run = store.create_run(
        conversation_id=conversation.conversation_id,
        status=RunStatus.RUNNING,
        prompt="review handoff",
        request_id="review-" + str(uuid4()),
        source=ConversationSource.CLI,
    )
    runtime = app.state.runtime.submit(
        "review handoff",
        "cli",
        request_id="handoff:" + run.run_id,
        mode="development",
        conversation_key=conversation.conversation_id,
    )
    domain = store.create_task_if_absent(
        conversation_id=conversation.conversation_id,
        task_id=runtime.task_id,
        title="review handoff",
        goal="review handoff",
        status=TaskStatus.IN_PROGRESS,
    )
    store.begin_handoff(
        run_id=run.run_id,
        conversation_id=conversation.conversation_id,
        request_id="handoff:" + run.run_id,
        prompt="review handoff",
        source="cli",
    )
    store.update_handoff(
        run.run_id,
        status=HandoffStatus.RUNTIME_CREATED,
        runtime_task_id=runtime.task_id,
    )
    store.update_handoff(
        run.run_id,
        status=HandoffStatus.DOMAIN_CREATED,
        domain_task_id=domain.task_id,
    )
    store.update_handoff(
        run.run_id,
        status=HandoffStatus.NEEDS_REVIEW,
        diagnostic="injected_handoff_failure",
    )
    return conversation, run, runtime, domain


def test_handoff_review_adopt_is_versioned_and_idempotent(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = tmp_path / "source"
        source.mkdir()
        initialize_repository(source)
        app = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="zzzz",
        )
        async with app.router.lifespan_context(app):
            conversation, run, _runtime, domain = _seed_handoff_review(app, source)
            api = RuntimeClient(
                "http://127.0.0.1:8777",
                "zzzz",
                transport=httpx.ASGITransport(app=app),
            )
            report = await api.get_handoff(run.run_id)
            assert report["handoff"]["status"] == "needs_review"
            assert report["domain_task"]["task_id"] == domain.task_id
            version = report["handoff"]["updated_at"]
            result = await api.review_handoff(
                run.run_id,
                {
                    "request_id": "review-adopt-1",
                    "action": "adopt_existing",
                    "expected_updated_at": version,
                    "note": "Task identity is verified",
                },
            )
            assert result["handoff"]["status"] == "completed"
            stored = app.state.conversations.store.get_run(run.run_id)
            assert stored is not None and stored.task_id == domain.task_id
            again = await api.review_handoff(
                run.run_id,
                {
                    "request_id": "review-adopt-1",
                    "action": "adopt_existing",
                    "expected_updated_at": version,
                    "note": "Task identity is verified",
                },
            )
            assert again["handoff"]["status"] == "completed"
            with pytest.raises(httpx.HTTPStatusError) as conflict:
                await api.review_handoff(
                    run.run_id,
                    {
                        "request_id": "review-adopt-stale",
                        "action": "adopt_existing",
                        "expected_updated_at": version,
                        "note": "stale version",
                    },
                )
            assert conflict.value.response.status_code == 409
            assert conversation.conversation_id == run.conversation_id

    asyncio.run(scenario())


def test_handoff_review_abandon_keeps_task_identity_and_cancels_active_task(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source = tmp_path / "source"
        source.mkdir()
        initialize_repository(source)
        app = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="zzzz",
        )
        async with app.router.lifespan_context(app):
            _, run, runtime, _ = _seed_handoff_review(app, source)
            api = RuntimeClient(
                "http://127.0.0.1:8777",
                "zzzz",
                transport=httpx.ASGITransport(app=app),
            )
            report = await api.get_handoff(run.run_id)
            result = await api.review_handoff(
                run.run_id,
                {
                    "request_id": "review-abandon-1",
                    "action": "abandon",
                    "expected_updated_at": report["handoff"]["updated_at"],
                    "note": "Cannot identify a safe continuation",
                },
            )
            assert result["handoff"]["status"] == "abandoned"
            stored = app.state.conversations.store.get_run(run.run_id)
            assert stored is not None and stored.status is RunStatus.CANCELLED
            assert app.state.runtime.inspect(runtime.task_id).task_id == runtime.task_id

    asyncio.run(scenario())


def test_handoff_review_requires_operator_authorization(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = tmp_path / "source"
        source.mkdir()
        initialize_repository(source)
        app = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="zzzz",
        )
        async with app.router.lifespan_context(app):
            _, run, _, _ = _seed_handoff_review(app, source)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1:8777",
            ) as client:
                response = await client.post(
                    f"/api/runtime/handoffs/{run.run_id}/review",
                    json={
                        "request_id": "unauthorized",
                        "action": "abandon",
                        "expected_updated_at": "ignored",
                        "note": "unauthorized",
                    },
                )
            assert response.status_code in {401, 403}
            record = app.state.conversations.store.get_handoff(run.run_id)
            assert record is not None and record.status is HandoffStatus.NEEDS_REVIEW

    asyncio.run(scenario())


class _FakeTasks:
    def __init__(
        self,
        task: RuntimeTask,
        *,
        cancel_result: RuntimeTask | None = None,
        slow: bool = False,
    ) -> None:
        self.task = task
        self.cancel_result = cancel_result or task.model_copy(
            update={"status": "cancelled"}
        )
        self.slow = slow
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False
        self.cancel_calls = 0

    def inspect(self, task_id: str) -> RuntimeTask:
        if task_id != self.task.task_id:
            raise KeyError(task_id)
        return self.task

    async def cancel(self, task_id: str) -> RuntimeTask:
        self.cancelled = True
        self.cancel_calls += 1
        self.started.set()
        if self.slow:
            await self.release.wait()
        return self.cancel_result


def _direct_review_setup(
    tmp_path: Path, *, task: RuntimeTask | None = None
) -> tuple[DomainStore, TaskService, Any, Any, Any, _FakeTasks]:
    source, store, tasks = _task_service(tmp_path)
    run = _run(store, source)
    domain = store.create_task_if_absent(
        conversation_id=run.conversation_id,
        task_id=task.task_id if task is not None else "task-review",
        title="review",
        goal="review",
        status=TaskStatus.IN_PROGRESS,
    )
    runtime = task or RuntimeTask(
        task_id=domain.task_id,
        prompt="review",
        source="cli",
        mode="development",
        request_id="handoff:" + run.run_id,
        conversation_key=run.conversation_id,
        status="queued",
    )
    store.begin_handoff(
        run_id=run.run_id,
        conversation_id=run.conversation_id,
        request_id="handoff:" + run.run_id,
        prompt="review",
        source="cli",
    )
    store.update_handoff(
        run.run_id,
        status=HandoffStatus.RUNTIME_CREATED,
        runtime_task_id=runtime.task_id,
    )
    store.update_handoff(
        run.run_id,
        status=HandoffStatus.DOMAIN_CREATED,
        domain_task_id=domain.task_id,
    )
    store.update_handoff(
        run.run_id,
        status=HandoffStatus.NEEDS_REVIEW,
        diagnostic="injected",
    )
    fake = _FakeTasks(runtime)
    handoff = DevelopmentHandoff(
        store,
        SimpleNamespace(worktrees=tmp_path / "worktrees"),  # type: ignore[arg-type]
        fake,  # type: ignore[arg-type]
    )
    return store, tasks, run, domain, handoff, fake


def test_abandon_rejects_runtime_task_identity_mismatch(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, tasks, run, domain, handoff, fake = _direct_review_setup(tmp_path)
        try:
            record = store.get_handoff(run.run_id)
            assert record is not None
            fake.task = fake.task.model_copy(update={"request_id": "wrong-request"})
            with pytest.raises(HandoffReviewConflict, match="identity mismatch"):
                await handoff.review(
                    run.run_id,
                    request_id="abandon-mismatch",
                    action="abandon",
                    expected_updated_at=record.updated_at,
                    note="must not cancel a foreign task",
                )
            assert not fake.cancelled
            current = store.get_handoff(run.run_id)
            assert current is not None and current.status is HandoffStatus.NEEDS_REVIEW
            stored_run = store.get_run(run.run_id)
            assert stored_run is not None and stored_run.status is RunStatus.RUNNING
            assert store.get_task(domain.task_id) is not None
        finally:
            await _close_tasks(tasks)

    asyncio.run(scenario())


def test_abandon_keeps_needs_review_when_cancel_result_is_unknown(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source, store, tasks = _task_service(tmp_path)
        try:
            run = _run(store, source)
            domain = store.create_task_if_absent(
                conversation_id=run.conversation_id,
                task_id="task-unknown",
                title="review",
                goal="review",
                status=TaskStatus.IN_PROGRESS,
            )
            runtime = RuntimeTask(
                task_id=domain.task_id,
                prompt="review",
                source="cli",
                mode="development",
                request_id="handoff:" + run.run_id,
                conversation_key=run.conversation_id,
                status="running",
            )
            store.begin_handoff(
                run_id=run.run_id,
                conversation_id=run.conversation_id,
                request_id="handoff:" + run.run_id,
                prompt="review",
                source="cli",
            )
            store.update_handoff(
                run.run_id,
                status=HandoffStatus.RUNTIME_CREATED,
                runtime_task_id=runtime.task_id,
            )
            store.update_handoff(
                run.run_id,
                status=HandoffStatus.DOMAIN_CREATED,
                domain_task_id=domain.task_id,
            )
            store.update_handoff(
                run.run_id,
                status=HandoffStatus.NEEDS_REVIEW,
                diagnostic="injected",
            )
            fake = _FakeTasks(
                runtime,
                cancel_result=runtime.model_copy(
                    update={"status": "recovery_required"}
                ),
            )
            handoff = DevelopmentHandoff(
                store,
                SimpleNamespace(worktrees=tmp_path / "worktrees"),  # type: ignore[arg-type]
                fake,  # type: ignore[arg-type]
            )
            record = store.get_handoff(run.run_id)
            assert record is not None
            with pytest.raises(HandoffReviewConflict, match="safely settled"):
                await handoff.review(
                    run.run_id,
                    request_id="abandon-unknown",
                    action="abandon",
                    expected_updated_at=record.updated_at,
                    note="unknown side effects",
                )
            assert fake.cancelled
            current = store.get_handoff(run.run_id)
            assert current is not None and current.status is HandoffStatus.NEEDS_REVIEW
            assert store.get_task(domain.task_id) is not None
            stored_run = store.get_run(run.run_id)
            assert stored_run is not None and stored_run.status is RunStatus.RUNNING
        finally:
            await _close_tasks(tasks)

    asyncio.run(scenario())


def test_concurrent_reviews_have_one_winner_and_stable_idempotency(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store, tasks, run, domain, handoff, fake = _direct_review_setup(tmp_path)
        fake.slow = True
        try:
            record = store.get_handoff(run.run_id)
            assert record is not None
            first = asyncio.create_task(
                handoff.review(
                    run.run_id,
                    request_id="concurrent-abandon",
                    action="abandon",
                    expected_updated_at=record.updated_at,
                    note="operator one",
                )
            )
            await fake.started.wait()
            with pytest.raises(HandoffReviewConflict, match="already in progress"):
                await handoff.review(
                    run.run_id,
                    request_id="concurrent-adopt",
                    action="adopt_existing",
                    expected_updated_at=record.updated_at,
                    note="operator two",
                )
            fake.release.set()
            result = await first
            assert result["handoff"]["status"] == "abandoned"
            again = await handoff.review(
                run.run_id,
                request_id="concurrent-abandon",
                action="abandon",
                expected_updated_at=record.updated_at,
                note="operator one",
            )
            assert again["handoff"]["status"] == "abandoned"
            with pytest.raises(HandoffReviewConflict, match="different input"):
                await handoff.review(
                    run.run_id,
                    request_id="concurrent-abandon",
                    action="abandon",
                    expected_updated_at=record.updated_at,
                    note="different operator note",
                )
            assert store.get_task(domain.task_id) is not None
        finally:
            await _close_tasks(tasks)

    asyncio.run(scenario())


def test_review_interrupted_by_crash_is_recovered_to_needs_review(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store, tasks, run, domain, handoff, fake = _direct_review_setup(tmp_path)
        try:
            record = store.get_handoff(run.run_id)
            assert record is not None
            store.begin_handoff_review(
                run.run_id,
                request_id="crash-review",
                action="abandon",
                expected_updated_at=record.updated_at,
                note="will be interrupted",
            )
            # Simulate a process crash after claim, before external work.
            assert store.recover_interrupted_handoff_reviews() == 1
            current = store.get_handoff(run.run_id)
            assert current is not None
            assert current.status is HandoffStatus.NEEDS_REVIEW
            assert current.diagnostic == "review_interrupted"
            recovered = handoff.recover_incomplete()
            assert recovered == ()
            assert store.get_task(domain.task_id) is not None
            assert not fake.cancelled
        finally:
            await _close_tasks(tasks)

    asyncio.run(scenario())


def test_review_cancellation_persists_needs_review_and_blocks_replay(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store, tasks, run, domain, handoff, fake = _direct_review_setup(tmp_path)
        fake.slow = True
        try:
            record = store.get_handoff(run.run_id)
            assert record is not None
            in_flight = asyncio.create_task(
                handoff.review(
                    run.run_id,
                    request_id="cancel-review",
                    action="abandon",
                    expected_updated_at=record.updated_at,
                    note="cancel during external cancel",
                )
            )
            await fake.started.wait()
            in_flight.cancel()
            with pytest.raises(asyncio.CancelledError):
                await in_flight
            current = store.get_handoff(run.run_id)
            assert current is not None
            assert current.status is HandoffStatus.NEEDS_REVIEW
            assert current.diagnostic == "review_cancelled"
            assert store.get_task(domain.task_id) is not None
            with pytest.raises(HandoffReviewFailed, match="review_cancelled"):
                await handoff.review(
                    run.run_id,
                    request_id="cancel-review",
                    action="abandon",
                    expected_updated_at=record.updated_at,
                    note="cancel during external cancel",
                )
            assert fake.cancel_calls == 1
        finally:
            await _close_tasks(tasks)

    asyncio.run(scenario())


def test_failed_review_replay_keeps_failure_semantics_without_side_effects(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store, tasks, run, domain, handoff, fake = _direct_review_setup(tmp_path)
        try:
            record = store.get_handoff(run.run_id)
            assert record is not None
            fake.task = fake.task.model_copy(update={"request_id": "wrong-request"})
            with pytest.raises(HandoffReviewConflict, match="identity mismatch"):
                await handoff.review(
                    run.run_id,
                    request_id="failed-replay",
                    action="abandon",
                    expected_updated_at=record.updated_at,
                    note="identity mismatch",
                )
            assert fake.cancel_calls == 0
            with pytest.raises(HandoffReviewFailed, match="identity mismatch"):
                await handoff.review(
                    run.run_id,
                    request_id="failed-replay",
                    action="abandon",
                    expected_updated_at=record.updated_at,
                    note="identity mismatch",
                )
            assert fake.cancel_calls == 0
            with pytest.raises(HandoffReviewConflict, match="different input"):
                await handoff.review(
                    run.run_id,
                    request_id="failed-replay",
                    action="abandon",
                    expected_updated_at=record.updated_at,
                    note="different note",
                )
            assert fake.cancel_calls == 0
            current = store.get_handoff(run.run_id)
            assert current is not None and current.status is HandoffStatus.NEEDS_REVIEW
            assert store.get_task(domain.task_id) is not None
        finally:
            await _close_tasks(tasks)

    asyncio.run(scenario())
