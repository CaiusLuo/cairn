import asyncio
import hashlib
import multiprocessing
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest

from cairn.core.events import Event
from cairn.core.models import LLMResponse, Message
from cairn.core.permissions import PermissionCapability, SessionPermissionHandler
from cairn.core.repository import RepositoryEvidence
from cairn.core.tasks import TaskSpec, TaskStatus
from cairn.infra.git.context import RepoContextProvider
from cairn.infra.sessions.models import SessionStoreError
from cairn.infra.sessions.task_models import TaskRecord, TaskRevision
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.agent import SessionAdmissionError
from cairn.runtime.session import SessionState
from cairn.runtime.tasks import CodingTaskRunner
from cairn.terminal.durable import open_store
from tests.support.durable_tasks import (
    BUDGET,
    SPEC,
    crash_task_writer,
    edit_response,
    make_host,
    reconcile_fresh,
)
from tests.support.git import git, initialize_repository
from tests.support.runtime import SequenceLLM
from tests.support.sessions import SESSION, UNVERIFIED_RUNTIME, legacy_store


@pytest.fixture
def root() -> Iterator[Path]:
    # Production roots include /tmp on both platforms. Never weaken validation
    # to accommodate Linux's pytest tmp_path; clean up even on failed assertions.
    with TemporaryDirectory(prefix="cairn-durable-task-", dir=Path.home()) as directory:
        yield Path(directory).resolve()


def test_stateless_still_creates_no_store(tmp_path: Path) -> None:
    llm = SequenceLLM([LLMResponse(content="done")])
    result = asyncio.run(
        CodingTaskRunner(workspace=Workspace(tmp_path), llm=llm, budget=BUDGET).run(
            SPEC
        )
    )
    assert result.status is TaskStatus.COMPLETED
    assert result.repository.is_git_repository is False
    assert list(tmp_path.iterdir()) == []
    assert len(llm.calls) == 1


def test_durable_task_commits_exact_turn_then_inspection_then_result(
    root: Path,
) -> None:
    phases: list[str] = []
    llm = SequenceLLM([edit_response(), LLMResponse(content="model claim")])

    def observe(event: Event) -> None:
        if event.type in ("turn_started", "tool_result", "turn_committed"):
            report = host.store.inspect_task(SESSION, SPEC.task_id)
            phases.append(report.task.phase)
            assert report.task.phase == "running" and report.task.result is None
            if event.type == "turn_committed":
                assert report.session_checkpoint.revision.head_seq == 1
                # Committed Turn callbacks do not relinquish Task ownership.
                with pytest.raises(SessionStoreError, match="task_still_owned"):
                    host.store.close()

    host = make_host(root, llm, event_handler=observe)
    original_handler = host.agent.event_handler
    original_refs = git(host.workspace.root, "show-ref")
    original_worktrees = git(host.workspace.root, "worktree", "list", "--porcelain")
    try:
        result = asyncio.run(host.runner.run(SPEC, durable_session=host.session))
        report = host.store.inspect_task(SESSION, SPEC.task_id)
        session = host.store.inspect(SESSION)
        assert (
            result.status is TaskStatus.COMPLETED
            and result.final_response == "model claim"
        )
        assert report.status == "result_recorded" and report.task.revision.version == 3
        assert (
            report.task.result is not None and report.task.result.status == "completed"
        )
        assert report.task.result.trace_id == result.trace_id
        assert report.task.checkpoint == session.receipt == host.session.receipt
        assert report.task.base_revision.head_seq == 0
        assert (
            report.task.input_digest == hashlib.sha256(SPEC.prompt.encode()).hexdigest()
        )
        assert len(session.attempts) == 1
        intent = session.attempts[0].intent
        assert intent.session_id == SESSION and intent.task_id == SPEC.task_id
        assert intent.input_digest == report.task.input_digest and intent.base_seq == 0
        assert len(session.attempts[0].diagnostics.fences) == 1
        assert report.external_effects_may_have_occurred
        assert result.repository.dirty is True
        assert report.task.result.repository is not None
        assert (
            report.task.result.repository.head_revision
            == result.repository.head_revision
        )
        assert (host.workspace.root / "tracked.txt").read_text() == "after\n"
        assert git(host.workspace.root, "show-ref") == original_refs
        assert (
            git(host.workspace.root, "worktree", "list", "--porcelain")
            == original_worktrees
        )
        assert len(llm.calls) == 2 and phases == ["running"] * 3
        assert host.agent.event_handler is original_handler
        # A subsequent Task requires a fresh host Agent/Session, not old grants.
        second = asyncio.run(
            host.runner.run(
                TaskSpec(task_id="second", prompt="other"), durable_session=host.session
            )
        )
        assert second.status is TaskStatus.RUNTIME_ERROR and len(llm.calls) == 2
    finally:
        host.session.close()


def test_fresh_reopen_inspects_without_execution_then_explicit_new_task(
    root: Path,
) -> None:
    host = make_host(root)
    asyncio.run(host.runner.run(SPEC, durable_session=host.session))
    first = host.store.inspect_task(SESSION, SPEC.task_id)
    host.session.close()
    llm = SequenceLLM([LLMResponse(content="second")])
    fresh = make_host(root, llm, reopen=True)
    try:
        assert fresh.session.state is SessionState.SUSPENDED
        assert fresh.store.inspect_task(SESSION, SPEC.task_id) == first
        assert llm.calls == [] and fresh.session.agent is not host.agent
        assert fresh.agent.permission_handler is not host.agent.permission_handler
        assert isinstance(fresh.agent.permission_handler, SessionPermissionHandler)
        assert fresh.agent.permission_handler.grants == set()
        reconcile_fresh(fresh)
        assert llm.calls == []
        result = asyncio.run(
            fresh.runner.run(
                TaskSpec(task_id="second", prompt="new input"),
                durable_session=fresh.session,
            )
        )
        assert result.status is TaskStatus.COMPLETED
        assert fresh.store.inspect_task(
            SESSION, "second"
        ).task.base_revision == first.session_revision.model_copy(
            update={"version": first.session_revision.version + 1}
        )
        assert fresh.store.inspect_task(SESSION, SPEC.task_id).task == first.task
        assert fresh.session.receipt.revision.head_seq == 2 and len(llm.calls) == 1
    finally:
        fresh.session.close()


def test_failed_inspection_retains_committed_turn_and_unfinished_task(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unavailable(
        self: RepoContextProvider, *, secret_env_keys: frozenset[str] = frozenset()
    ) -> RepositoryEvidence:
        assert host.store.inspect_task(SESSION, SPEC.task_id).task.phase == "inspecting"
        raise RuntimeError("private inspection secret")

    monkeypatch.setattr(RepoContextProvider, "inspect_evidence", unavailable)
    llm = SequenceLLM([LLMResponse(content="done")])
    host = make_host(root, llm)
    try:
        result = asyncio.run(host.runner.run(SPEC, durable_session=host.session))
        report = host.store.inspect_task(SESSION, SPEC.task_id)
        assert (
            result.status is TaskStatus.RUNTIME_ERROR
            and result.final_response == "done"
        )
        assert report.task.phase == "inspecting" and report.task.result is None
        assert report.task.checkpoint == host.session.receipt
        assert report.task.checkpoint.outcome == "completed"
        assert (
            report.task.diagnostic == "inspection_failed"
            and report.status == "recovery_required"
        )
        assert "private" not in report.model_dump_json() and "private" not in (
            result.error or ""
        )
        assert len(llm.calls) == 1
    finally:
        host.session.close()


@pytest.mark.parametrize("when", ["before", "during"])
@pytest.mark.parametrize("identity", ["head", "workspace"])
def test_inspection_rejects_current_binding_drift(
    root: Path, monkeypatch: pytest.MonkeyPatch, when: str, identity: str
) -> None:
    inspected = 0

    def change() -> None:
        if identity == "head":
            git(host.workspace.root, "commit", "--allow-empty", "-m", "new head")
        else:
            host.workspace.root.rename(root / "old-work")
            host.workspace.root.mkdir()
            initialize_repository(host.workspace.root)

    def observe(event: Event) -> None:
        if when == "before" and event.type == "turn_committed":
            change()

    original = RepoContextProvider.inspect_evidence

    async def inspect(
        self: RepoContextProvider, *, secret_env_keys: frozenset[str] = frozenset()
    ) -> RepositoryEvidence:
        nonlocal inspected
        inspected += 1
        result = await original(self, secret_env_keys=secret_env_keys)
        if when == "during":
            change()
        return result

    monkeypatch.setattr(RepoContextProvider, "inspect_evidence", inspect)
    host = make_host(root, event_handler=observe)
    try:
        result = asyncio.run(host.runner.run(SPEC, durable_session=host.session))
        report = host.store.inspect_task(SESSION, SPEC.task_id)
        assert result.status is TaskStatus.RUNTIME_ERROR
        assert report.task.phase == "inspecting" and report.task.result is None
        assert report.task.diagnostic == "inspection_failed"
        assert report.task.checkpoint == host.session.receipt
        assert report.session_checkpoint.outcome == "completed"
        assert inspected == int(when == "during")
    finally:
        host.session.close()


def test_duplicate_task_id_on_fresh_binding_never_repeats_execution(root: Path) -> None:
    host = make_host(root)
    try:
        result = asyncio.run(host.runner.run(SPEC, durable_session=host.session))
        assert result.status is TaskStatus.COMPLETED
        task = host.store.inspect_task(SESSION, SPEC.task_id).task
    finally:
        host.session.close()
    llm = SequenceLLM([])
    fresh = make_host(root, llm, reopen=True)
    try:
        reconcile_fresh(fresh)
        result = asyncio.run(fresh.runner.run(SPEC, durable_session=fresh.session))
        assert result.status is TaskStatus.RUNTIME_ERROR and llm.calls == []
        assert fresh.store.inspect_task(SESSION, SPEC.task_id).task == task
        assert fresh.store.inspect(SESSION).checkpoint.seq == 1
        assert (fresh.workspace.root / "tracked.txt").read_text() == "after\n"
    finally:
        fresh.session.close()


@pytest.mark.parametrize(
    "phase",
    [
        "create_task",
        "begin_turn",
        "commit_task_turn",
        "start_task_inspection",
        "record_task_result",
    ],
)
@pytest.mark.parametrize("after", [False, True])
def test_cancellation_at_synchronous_task_boundaries(
    root: Path, monkeypatch: pytest.MonkeyPatch, phase: str, after: bool
) -> None:
    llm = SequenceLLM([edit_response(), LLMResponse(content="done")])
    host = make_host(root, llm)
    original = getattr(host.store, phase)

    def cancelled(*args: Any, **kwargs: Any) -> object:
        if after:
            original(*args, **kwargs)
        raise asyncio.CancelledError("host interruption")

    monkeypatch.setattr(host.store, phase, cancelled)
    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(host.runner.run(SPEC, durable_session=host.session))
        session = host.store.inspect(SESSION)
        if phase == "create_task" and not after:
            with pytest.raises(SessionStoreError, match="task_missing"):
                host.store.inspect_task(SESSION, SPEC.task_id)
        else:
            report = host.store.inspect_task(SESSION, SPEC.task_id)
            if phase == "record_task_result" and after:
                assert (
                    report.task.phase == "result_recorded"
                    and report.task.result is not None
                )
            else:
                assert report.task.result is None and report.task.recovery_required
                assert report.task.diagnostic in ("interrupted", "storage_failed")
        assert len(session.attempts) <= 1 and session.checkpoint.seq <= 1
        assert len(llm.calls) == (0 if phase in ("create_task", "begin_turn") else 2)
        assert (host.workspace.root / "tracked.txt").read_text() == (
            "before\n" if not llm.calls else "after\n"
        )
    finally:
        host.session.close()


def test_cooperative_cancel_before_admission_has_no_task(root: Path) -> None:
    llm = SequenceLLM([])
    host = make_host(root, llm)
    stop = asyncio.Event()
    stop.set()
    try:
        result = asyncio.run(
            host.runner.run(SPEC, cancellation_event=stop, durable_session=host.session)
        )
        assert result.status is TaskStatus.CANCELLED and llm.calls == []
        assert host.store.inspect(SESSION).attempts == ()
        with pytest.raises(SessionStoreError, match="task_missing"):
            host.store.inspect_task(SESSION, SPEC.task_id)
    finally:
        host.session.close()


@pytest.mark.parametrize("after_effect", [False, True])
def test_cooperative_cancellation_records_only_a_cancelled_result(
    root: Path, after_effect: bool
) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        stop = asyncio.Event()

        class WaitingLLM:
            calls = 0

            async def generate(
                self, messages: list[Message], tools: list[dict[str, Any]] | None = None
            ) -> LLMResponse:
                self.calls += 1
                if after_effect and self.calls == 1:
                    return edit_response()
                started.set()
                await asyncio.Event().wait()
                raise AssertionError("Cancelled model execution must not resume")

        llm = WaitingLLM()
        host = make_host(root, llm)
        pending = asyncio.create_task(
            host.runner.run(SPEC, cancellation_event=stop, durable_session=host.session)
        )
        try:
            await started.wait()
            stop.set()
            result = await pending
            report = host.store.inspect_task(SESSION, SPEC.task_id)
            assert result.status is TaskStatus.CANCELLED and result.error is None
            assert report.task.phase == "result_recorded"
            assert report.task.result is not None
            assert report.task.result.status == "cancelled"
            assert report.task.checkpoint == host.session.receipt
            assert host.session.receipt.outcome == "cancelled"
            assert not report.task.recovery_required
            assert report.external_effects_may_have_occurred == after_effect
            assert llm.calls == 1 + int(after_effect)
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            host.session.close()

    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_budget_abort_records_a_noncompleted_task_result(root: Path) -> None:
    from cairn.core.budget import RunBudget

    llm = SequenceLLM([edit_response()])
    host = make_host(root, llm)
    host.runner.budget = RunBudget(max_steps=1)
    try:
        result = asyncio.run(host.runner.run(SPEC, durable_session=host.session))
        report = host.store.inspect_task(SESSION, SPEC.task_id)
        assert result.status is TaskStatus.BUDGET_EXHAUSTED
        assert report.task.phase == "result_recorded" and report.task.result is not None
        assert report.task.result.status == "budget_exhausted"
        assert host.session.receipt.outcome == "budget_exhausted"
        assert len(llm.calls) == 1
        assert (host.workspace.root / "tracked.txt").read_text() == "after\n"
    finally:
        host.session.close()


@pytest.mark.parametrize("boundary", ["model", "effect", "inspection"])
@pytest.mark.parametrize("external", [False, True])
def test_cancellation_retains_writer_until_cleanup_settles(
    root: Path, monkeypatch: pytest.MonkeyPatch, boundary: str, external: bool
) -> None:
    async def scenario() -> None:
        started, cleaning, finish, settled = (asyncio.Event() for _ in range(4))
        stop = asyncio.Event()

        async def wait_for_cancel() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await finish.wait()
                settled.set()

        class SlowLLM:
            calls = 0

            async def generate(
                self, messages: list[Message], tools: list[dict[str, Any]] | None = None
            ) -> LLMResponse:
                self.calls += 1
                if boundary == "model" or (boundary == "effect" and self.calls == 2):
                    await wait_for_cancel()
                return (
                    edit_response() if self.calls == 1 else LLMResponse(content="done")
                )

        llm = SlowLLM()
        host = make_host(root, llm)
        if boundary == "inspection":

            async def inspect(
                self: RepoContextProvider,
                *,
                secret_env_keys: frozenset[str] = frozenset(),
            ) -> RepositoryEvidence:
                await wait_for_cancel()
                return RepositoryEvidence(workspace_root=self.workspace.root)

            monkeypatch.setattr(RepoContextProvider, "inspect_evidence", inspect)
        baseline = asyncio.all_tasks()
        pending = asyncio.create_task(
            host.runner.run(
                SPEC,
                cancellation_event=None if external else stop,
                durable_session=host.session,
            )
        )
        try:
            await started.wait()
            if external or boundary == "inspection":
                pending.cancel("primary cancellation")
            else:
                stop.set()
            await cleaning.wait()
            pending.cancel("repeated cancellation")
            await asyncio.sleep(0)
            host.session.close()
            with pytest.raises(SessionStoreError, match=r"(turn|task)_still_owned"):
                host.store.close()
            with pytest.raises(SessionStoreError, match="writer_busy"):
                open_store(
                    root / "store", host.workspace, allow_unverified=UNVERIFIED_RUNTIME
                )
            assert not pending.done() and not settled.is_set()
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert settled.is_set() and asyncio.all_tasks() == baseline
            reader = open_store(
                root / "store",
                host.workspace,
                inspect=True,
                allow_unverified=UNVERIFIED_RUNTIME,
            )
            try:
                report = reader.inspect_task(SESSION, SPEC.task_id)
                assert (
                    report.task.result is None
                    and report.task.diagnostic == "interrupted"
                )
                assert report.task.phase == (
                    "inspecting" if boundary == "inspection" else "turn_committed"
                )
                assert report.session_checkpoint.revision.head_seq == 1
                assert report.external_effects_may_have_occurred == (
                    boundary != "model"
                )
            finally:
                reader.close()
        finally:
            finish.set()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            host.session.close()

    asyncio.run(asyncio.wait_for(scenario(), 10))


@pytest.mark.parametrize(
    "phase",
    [
        "create_task",
        "commit_task_turn",
        "start_task_inspection",
        "record_task_result",
        "commit_turn",
    ],
)
@pytest.mark.parametrize("committed", [False, True])
def test_unknown_commit_observes_facts_without_mutation_or_execution_retry(
    root: Path, monkeypatch: pytest.MonkeyPatch, phase: str, committed: bool
) -> None:
    llm = SequenceLLM([edit_response(), LLMResponse(content="done")])
    host = make_host(root, llm)
    original = getattr(host.store, phase)
    original_commit = host.store._commit
    calls = 0

    def uncertain(conn: sqlite3.Connection) -> None:
        nonlocal calls
        calls += 1
        if committed:
            conn.commit()
        raise sqlite3.OperationalError("private unknown acknowledgement")

    def write(*args: Any, **kwargs: Any) -> object:
        monkeypatch.setattr(host.store, "_commit", uncertain)
        try:
            return original(*args, **kwargs)
        finally:
            monkeypatch.setattr(host.store, "_commit", original_commit)

    monkeypatch.setattr(host.store, phase, write)
    try:
        result = asyncio.run(host.runner.run(SPEC, durable_session=host.session))
        assert result.status is TaskStatus.RUNTIME_ERROR and calls == 1
        assert "private" not in (result.error or "")
        assert len(llm.calls) == (0 if phase == "create_task" else 2)
        session = host.store.inspect(SESSION)
        assert len(session.attempts) == (0 if phase == "create_task" else 1)
        if phase == "create_task" and not committed:
            with pytest.raises(SessionStoreError, match="task_missing"):
                host.store.inspect_task(SESSION, SPEC.task_id)
        else:
            report = host.store.inspect_task(SESSION, SPEC.task_id)
            if phase == "record_task_result" and committed:
                assert (
                    report.task.phase == "result_recorded"
                    and report.task.result is not None
                )
            else:
                assert report.task.result is None and report.task.recovery_required
            assert "private" not in report.model_dump_json()
        assert session.checkpoint.seq == (
            0
            if phase == "create_task"
            else int(committed)
            if phase == "commit_turn"
            else 1
        )
    finally:
        host.session.close()


@pytest.mark.parametrize(
    "invalid",
    [
        "head",
        "workspace",
        "git_identity",
        "provider",
        "runtime",
        "permissions",
        "runner_workspace",
    ],
)
def test_current_binding_rejection_precedes_task_and_model(
    root: Path, invalid: str
) -> None:
    llm = SequenceLLM([])
    host = make_host(root, llm)
    if invalid == "head":
        (host.workspace.root / "tracked.txt").write_text("host change\n")
        git(host.workspace.root, "add", "tracked.txt")
        git(host.workspace.root, "commit", "-m", "host change")
    elif invalid == "workspace":
        host.workspace.root.rename(root / "old-workspace")
        host.workspace.root.mkdir()
        initialize_repository(host.workspace.root)
    elif invalid == "git_identity":
        (host.workspace.root / ".git").rename(root / "old-git")
        initialize_repository(host.workspace.root)
    elif invalid == "provider":
        host.agent.provider_name = "unapproved-provider"
    elif invalid == "runtime":
        host.agent.llm = SequenceLLM([])
    elif invalid == "permissions":
        assert isinstance(host.agent.permission_handler, SessionPermissionHandler)
        host.agent.permission_handler.grants.add(PermissionCapability.NETWORK)
    else:
        other = root / "other"
        other.mkdir()
        host.runner.workspace = Workspace(other)
    try:
        result = asyncio.run(host.runner.run(SPEC, durable_session=host.session))
        assert result.status is TaskStatus.RUNTIME_ERROR
        assert result.repository.is_git_repository is None
        assert llm.calls == [] and host.store.inspect(SESSION).attempts == ()
        with pytest.raises(SessionStoreError, match="task_missing"):
            host.store.inspect_task(SESSION, SPEC.task_id)
    finally:
        host.session.close()


@pytest.mark.parametrize(
    "invalid", ["empty_prompt", "long_id", "secret_id", "empty_id"]
)
def test_invalid_or_sensitive_spec_is_not_admitted(root: Path, invalid: str) -> None:
    llm = SequenceLLM([])
    host = make_host(root, llm, sensitive_values=("known-private-value",))
    spec = SPEC.model_copy(
        update={"prompt": ""}
        if invalid == "empty_prompt"
        else {
            "task_id": "x" * 257
            if invalid == "long_id"
            else "known-private-value"
            if invalid == "secret_id"
            else ""
        }
    )
    try:
        result = asyncio.run(host.runner.run(spec, durable_session=host.session))
        assert result.status is TaskStatus.RUNTIME_ERROR and llm.calls == []
        assert (
            host.store._conn().execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
        )
        assert host.store.inspect(SESSION).attempts == ()
    finally:
        host.session.close()


@pytest.mark.parametrize("invalid", ["task", "session", "digest", "revision", "prompt"])
def test_typed_turn_attribution_rejects_mismatches(root: Path, invalid: str) -> None:
    from cairn.runtime.loop import run_turn

    llm = SequenceLLM([])
    host = make_host(root, llm)
    digest = hashlib.sha256(SPEC.prompt.encode()).hexdigest()
    task = host.store.create_task(
        host.session.revision, task_id=SPEC.task_id, input_digest=digest
    )
    attributed = task
    if invalid == "task":
        attributed = task.model_copy(
            update={"revision": task.revision.model_copy(update={"task_id": "other"})}
        )
    elif invalid == "session":
        attributed = task.model_copy(
            update={
                "revision": task.revision.model_copy(
                    update={"session_id": "00000000-0000-4000-8000-000000000009"}
                )
            }
        )
    elif invalid == "digest":
        attributed = task.model_copy(update={"input_digest": "0" * 64})
    elif invalid == "revision":
        attributed = task.model_copy(
            update={"revision": task.revision.model_copy(update={"version": 1})}
        )
    try:
        with (
            pytest.raises((SessionAdmissionError, SessionStoreError)),
            host.session.task_execution(attributed),
        ):
            asyncio.run(
                run_turn(
                    host.agent,
                    "different prompt" if invalid == "prompt" else SPEC.prompt,
                    budget=BUDGET,
                    session=host.session,
                )
            )
        assert llm.calls == [] and host.store.inspect(SESSION).attempts == ()
    finally:
        host.session.close()


def test_schema_one_rejects_without_implicit_upgrade(root: Path) -> None:
    legacy_store(root, version=1).close()
    host = make_host(root, SequenceLLM([]))
    path = host.store.path
    host.session.close()
    llm = SequenceLLM([])
    fresh = make_host(root, llm, reopen=True)
    try:
        reconcile_fresh(fresh)
        result = asyncio.run(fresh.runner.run(SPEC, durable_session=fresh.session))
        assert result.status is TaskStatus.RUNTIME_ERROR and llm.calls == []
        assert fresh.store.schema_version == 1
        assert fresh.store.inspect(SESSION).attempts == ()
        assert not list(path.parent.glob("schema-1-backup-*"))
    finally:
        fresh.session.close()


def test_task_cas_failure_retains_committed_turn_without_replay(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = SequenceLLM([edit_response(), LLMResponse(content="done")])
    host = make_host(root, llm)
    original = host.store.commit_task_turn

    def stale(expected: TaskRevision, receipt: Any) -> TaskRecord:
        return original(
            expected.model_copy(update={"version": expected.version + 1}), receipt
        )

    monkeypatch.setattr(host.store, "commit_task_turn", stale)
    try:
        result = asyncio.run(host.runner.run(SPEC, durable_session=host.session))
        report = host.store.inspect_task(SESSION, SPEC.task_id)
        assert result.status is TaskStatus.RUNTIME_ERROR and len(llm.calls) == 2
        assert report.session_checkpoint.revision.head_seq == 1
        assert report.task.phase == "running" and report.task.result is None
        assert report.task.recovery_required and report.task.revision.version == 1
        assert (host.workspace.root / "tracked.txt").read_text() == "after\n"
    finally:
        host.session.close()


def test_task_result_never_serializes_provider_output_or_exception_text(
    root: Path,
) -> None:
    class PrivateLLM:
        async def generate(
            self, messages: list[Message], tools: list[dict[str, Any]] | None = None
        ) -> LLMResponse:
            raise RuntimeError("private credential value")

    host = make_host(root, PrivateLLM(), sensitive_values=("private credential value",))
    try:
        result = asyncio.run(host.runner.run(SPEC, durable_session=host.session))
        report = host.store.inspect_task(SESSION, SPEC.task_id)
        assert result.status is TaskStatus.RUNTIME_ERROR
        assert report.task.phase == "result_recorded" and report.task.result is not None
        assert report.task.result.status == "runtime_error"
        assert (
            report.task.checkpoint is not None
            and report.task.checkpoint.outcome == "runtime_error"
        )
        assert "private" not in report.model_dump_json()
        assert "private" not in (result.error or "")
        assert (
            not {"error", "final_response", "prompt", "worktree"}
            & report.task.result.model_dump().keys()
        )
    finally:
        host.session.close()


@pytest.mark.parametrize(
    "window",
    [
        "before_admission",
        "admitted",
        "model",
        "fenced",
        "effect",
        "checkpoint",
        "inspecting",
        "inspection_done",
        "result_commit",
    ],
)
def test_process_death_exposes_facts_without_execution_or_automatic_reconciliation(
    root: Path, window: str
) -> None:
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    worker = ctx.Process(target=crash_task_writer, args=(str(root), window, child))
    worker.start()
    try:
        assert parent.poll(20) and parent.recv() == window
        workspace = Workspace(root / "work")
        with pytest.raises(SessionStoreError, match="writer_busy"):
            open_store(root / "store", workspace, allow_unverified=UNVERIFIED_RUNTIME)
        worker.kill()
        worker.join(10)
        assert worker.exitcode is not None
        before = {p.name: p.read_bytes() for p in (root / "store").iterdir()}
        reader = open_store(
            root / "store",
            workspace,
            inspect=True,
            allow_unverified=UNVERIFIED_RUNTIME,
        )
        try:
            session = reader.inspect(SESSION)
            post_turn = window in (
                "checkpoint",
                "inspecting",
                "inspection_done",
                "result_commit",
            )
            assert session.checkpoint.seq == int(post_turn)
            if window == "before_admission":
                with pytest.raises(SessionStoreError, match="task_missing"):
                    reader.inspect_task(SESSION, SPEC.task_id)
            else:
                report = reader.inspect_task(SESSION, SPEC.task_id)
                assert report.task.phase == (
                    "result_recorded"
                    if window == "result_commit"
                    else "inspecting"
                    if window in ("inspecting", "inspection_done")
                    else "running"
                )
                assert (report.task.result is not None) == (window == "result_commit")
                assert report.external_effects_may_have_occurred == (
                    window not in ("admitted", "model")
                )
                assert report.session_checkpoint == session.receipt
                if report.task.checkpoint is not None:
                    assert report.task.checkpoint == session.receipt
        finally:
            reader.close()
        assert {p.name: p.read_bytes() for p in (root / "store").iterdir()} == before
        llm = SequenceLLM([])
        fresh = make_host(root, llm, reopen=True)
        try:
            assert bool(fresh.session.agent.state.messages) == post_turn
            result = asyncio.run(fresh.runner.run(SPEC, durable_session=fresh.session))
            assert result.status is TaskStatus.RUNTIME_ERROR and llm.calls == []
            assert fresh.store.inspect(SESSION).checkpoint.seq == int(post_turn)
            if window != "before_admission":
                assert (
                    fresh.store.inspect_task(SESSION, SPEC.task_id).task == report.task
                )
        finally:
            fresh.session.close()
        assert (root / "work" / "tracked.txt").read_text() == (
            "after\n"
            if window
            in (
                "effect",
                "checkpoint",
                "inspecting",
                "inspection_done",
                "result_commit",
            )
            else "before\n"
        )
    finally:
        if worker.is_alive():
            worker.kill()
            worker.join(10)
        worker.close()
        parent.close()
        child.close()
