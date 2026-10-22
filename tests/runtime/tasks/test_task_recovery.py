"""Manual Task recovery uses stored receipts and fresh Git facts, never replay."""

import asyncio
import multiprocessing
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal, cast

import pytest

from cairn.core.permissions import PermissionCapability, SessionPermissionHandler
from cairn.core.repository import RepositoryEvidence
from cairn.infra.git.context import RepoContextProvider
from cairn.infra.sessions.models import SCHEMA_VERSION, SessionStoreError
from cairn.infra.sessions.task_models import TaskRecoveryReport, TaskResolution
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.agent import SessionAdmissionError
from cairn.terminal.durable import open_store
from tests.support.durable_tasks import (
    SPEC,
    DurableTaskHost,
    crash_task_writer,
    make_host,
    reconcile_fresh,
)
from tests.support.git import (
    configure_missing_promisor_tree,
    configure_side_effect_filter,
    git,
    initialize_repository,
)
from tests.support.runtime import SequenceLLM
from tests.support.sessions import SESSION, UNVERIFIED_RUNTIME


@pytest.fixture
def root() -> Iterator[Path]:
    with TemporaryDirectory(
        prefix="cairn-task-recovery-", dir=Path.home()
    ) as directory:
        yield Path(directory).resolve()


def decision(
    report: TaskRecoveryReport,
    action: Literal[
        "abandon_without_replay", "acknowledge_checkpoint", "inspect_repository"
    ],
) -> TaskResolution:
    from datetime import UTC, datetime

    return TaskResolution(
        task_revision=report.task.revision,
        session_revision=report.session_revision,
        input_digest=report.task.input_digest,
        action=action,
        actor="offline-human-host",
        resolved_at_utc=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )


def crash(root: Path, window: str) -> None:
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    worker = ctx.Process(target=crash_task_writer, args=(str(root), window, child))
    worker.start()
    child.close()
    try:
        assert parent.poll(15) and parent.recv() == window
        worker.kill()
        worker.join(15)
        assert worker.exitcode is not None
    finally:
        if worker.is_alive():
            worker.kill()
            worker.join(15)
        worker.close()
        parent.close()


def recover(
    host: DurableTaskHost,
    action: Literal[
        "abandon_without_replay", "acknowledge_checkpoint", "inspect_repository"
    ],
) -> TaskRecoveryReport:
    report = host.store.inspect_task(SESSION, SPEC.task_id)
    return asyncio.run(
        host.runner.reconcile_task(
            host.session, report, resolution=decision(report, action)
        )
    )


@pytest.mark.parametrize(
    "window", ["admitted", "effect", "checkpoint", "inspecting", "result_commit"]
)
def test_sigkill_recovery_preserves_git_and_receipts_without_turn_or_tool_replay(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    window: str,
) -> None:
    crash(root, window)
    llm = SequenceLLM([])
    fresh = make_host(root, llm, reopen=True)
    before_head = git(fresh.workspace.root, "rev-parse", "HEAD")
    before_index = (fresh.workspace.root / ".git" / "index").read_bytes()
    before_file = (fresh.workspace.root / "tracked.txt").read_bytes()
    before = fresh.store.inspect_task(SESSION, SPEC.task_id)
    receipt = before.session_checkpoint

    async def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Recovery dispatched an Agent Turn or tool")

    monkeypatch.setattr("cairn.runtime.tasks.runner.run_turn", forbidden)
    monkeypatch.setattr(fresh.agent.tools, "execute", forbidden)
    try:
        if window == "result_commit":
            after = recover(fresh, "inspect_repository")
            assert after == before  # Read-only even while Session is suspended.
        else:
            # Reopen alone grants neither Session nor Task recovery authority.
            action: Literal[
                "abandon_without_replay", "acknowledge_checkpoint", "inspect_repository"
            ] = (
                "abandon_without_replay"
                if window in ("admitted", "effect")
                else "acknowledge_checkpoint"
                if window == "checkpoint"
                else "inspect_repository"
            )
            with pytest.raises((SessionAdmissionError, SessionStoreError)):
                recover(fresh, action)
            reconcile_fresh(fresh)
            after = recover(fresh, action)
            if action == "acknowledge_checkpoint":
                assert (
                    after.task.phase == "turn_committed" and after.task.result is None
                )
                assert after.task.checkpoint == receipt
                after = recover(fresh, "inspect_repository")
            if window in ("admitted", "effect"):
                assert after.status == "abandoned" and after.task.result is None
                assert after.external_effects_may_have_occurred == (window == "effect")
                if window == "effect":
                    attempts = fresh.session.inspect().attempts
                    assert (
                        attempts[0].state == "abandoned"
                        and attempts[0].diagnostics.fences
                    )
            else:
                assert after.status == "result_recorded"
                assert (
                    after.task.result is not None
                    and after.task.result.status == "completed"
                )
                assert after.task.result.repository is not None
                assert after.task.result.repository.head_revision == before_head
                assert after.task.checkpoint == receipt
        assert llm.calls == []
        assert fresh.session.inspect().receipt == receipt
        assert git(fresh.workspace.root, "rev-parse", "HEAD") == before_head
        assert (fresh.workspace.root / ".git" / "index").read_bytes() == before_index
        assert (fresh.workspace.root / "tracked.txt").read_bytes() == before_file
        assert fresh.store.schema_version == SCHEMA_VERSION
    finally:
        fresh.session.close()


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
        "stale_task",
        "stale_session",
        "input",
    ],
)
def test_fresh_approval_and_binding_are_required_before_inspection(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid: str,
) -> None:
    crash(root, "inspecting")
    llm = SequenceLLM([])
    host = make_host(root, llm, reopen=True)
    reconcile_fresh(host)
    report = host.store.inspect_task(SESSION, SPEC.task_id)
    approval = decision(report, "inspect_repository")
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
        host.agent.provider_name = "unapproved"
    elif invalid == "runtime":
        host.agent.llm = SequenceLLM([])
    elif invalid == "permissions":
        assert isinstance(host.agent.permission_handler, SessionPermissionHandler)
        host.agent.permission_handler.grants.add(PermissionCapability.NETWORK)
    elif invalid == "runner_workspace":
        other = root / "other"
        other.mkdir()
        host.runner.workspace = Workspace(other)
    elif invalid == "stale_task":
        host.store.require_task_recovery(report.task.revision, "interrupted")
    elif invalid == "stale_session":
        reconcile_fresh(host)
    else:
        approval = approval.model_copy(update={"input_digest": "0" * 64})
    before = host.store.inspect_task(SESSION, SPEC.task_id)

    async def forbidden(*args: Any, **kwargs: Any) -> RepositoryEvidence:
        pytest.fail("Invalid recovery dispatched repository inspection")

    monkeypatch.setattr(RepoContextProvider, "inspect_evidence", forbidden)
    try:
        with pytest.raises((SessionAdmissionError, SessionStoreError)):
            asyncio.run(
                host.runner.reconcile_task(host.session, report, resolution=approval)
            )
        assert host.store.inspect_task(SESSION, SPEC.task_id).task == before.task
        assert llm.calls == []
    finally:
        host.session.close()


def test_recovery_inspection_preserves_stale_index_and_clean_file_bytes(
    root: Path,
) -> None:
    crash(root, "inspecting")
    host = make_host(root, SequenceLLM([]), reopen=True)
    tracked = host.workspace.root / "tracked.txt"
    tracked.write_bytes(b"before\n")
    stamp = tracked.stat()
    os.utime(tracked, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 5_000_000_000))
    reconcile_fresh(host)
    before = host.store.inspect_task(SESSION, SPEC.task_id)
    index = host.workspace.root / ".git" / "index"
    before_index = index.read_bytes()
    head = git(host.workspace.root, "rev-parse", "HEAD")
    try:
        after = recover(host, "inspect_repository")
        assert after.status == "result_recorded" and after.task.result is not None
        assert after.task.result.repository is not None
        assert after.task.result.repository.dirty is False
        assert after.task.checkpoint == before.task.checkpoint
        assert tracked.read_bytes() == b"before\n"
        assert index.read_bytes() == before_index
        assert git(host.workspace.root, "rev-parse", "HEAD") == head
    finally:
        host.session.close()


@pytest.mark.parametrize("unsafe", ["filter", "submodule", "promisor"])
def test_recovery_refuses_git_programs_without_result_or_effects(
    root: Path, unsafe: str
) -> None:
    crash(root, "inspecting")
    work = root / "work"
    marker = root / "outside-workspace-effect"
    if unsafe == "promisor":
        configure_missing_promisor_tree(work, marker)
    else:
        target = work
        if unsafe == "submodule":
            source = root / "child-source"
            source.mkdir()
            initialize_repository(source)
            git(
                work,
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                str(source),
                "nested",
            )
            target = work / "nested"
        configure_side_effect_filter(target, marker)
    llm = SequenceLLM([])
    host = make_host(root, llm, reopen=True)
    reconcile_fresh(host)
    before = host.store.inspect_task(SESSION, SPEC.task_id)
    index = (work / ".git" / "index").read_bytes()
    head = git(work, "rev-parse", "HEAD")
    tracked = (work / "tracked.txt").read_bytes()
    try:
        assert not marker.exists()
        with pytest.raises(SessionStoreError, match="task_inspection_unavailable"):
            recover(host, "inspect_repository")
        after = host.store.inspect_task(SESSION, SPEC.task_id)
        assert after.task.result is None and after.task.recovery_required
        assert after.task.diagnostic == "inspection_failed"
        assert after.task.checkpoint == before.task.checkpoint
        assert host.session.inspect().receipt == before.session_checkpoint
        assert not marker.exists() and llm.calls == []
        assert (work / ".git" / "index").read_bytes() == index
        assert git(work, "rev-parse", "HEAD") == head
        assert (work / "tracked.txt").read_bytes() == tracked
    finally:
        host.session.close()


@pytest.mark.parametrize("committed", [False, True])
def test_uncertain_result_commit_reads_actual_facts_without_retry(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    committed: bool,
) -> None:
    crash(root, "inspecting")
    host = make_host(root, SequenceLLM([]), reopen=True)
    reconcile_fresh(host)
    original = host.store.record_task_result
    commit = host.store._commit
    writes = 0

    def uncertain(conn: sqlite3.Connection) -> None:
        nonlocal writes
        writes += 1
        if committed:
            conn.commit()
        raise sqlite3.OperationalError("private-unknown-reply")

    def record(*args: Any, **kwargs: Any) -> object:
        monkeypatch.setattr(host.store, "_commit", uncertain)
        try:
            return original(*args, **kwargs)
        finally:
            monkeypatch.setattr(host.store, "_commit", commit)

    monkeypatch.setattr(host.store, "record_task_result", record)
    try:
        after = recover(host, "inspect_repository")
        assert writes == 1 and after.status == "recovery_required"
        assert (after.task.result is not None) == committed
        assert after.task.phase == ("result_recorded" if committed else "inspecting")
        assert "private" not in after.model_dump_json()
    finally:
        host.session.close()


def test_cancellation_holds_session_and_writer_through_repeated_cleanup(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crash(root, "inspecting")
    host = make_host(root, SequenceLLM([]), reopen=True)
    reconcile_fresh(host)

    async def scenario() -> None:
        started, cleaning, finish, settled = (asyncio.Event() for _ in range(4))

        async def inspect(*args: Any, **kwargs: Any) -> RepositoryEvidence:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await finish.wait()
                settled.set()
            raise AssertionError("Cancelled inspection resumed")

        monkeypatch.setattr(RepoContextProvider, "inspect_evidence", inspect)
        report = host.store.inspect_task(SESSION, SPEC.task_id)
        baseline = asyncio.all_tasks()
        pending = asyncio.create_task(
            host.runner.reconcile_task(
                host.session, report, resolution=decision(report, "inspect_repository")
            )
        )
        try:
            await started.wait()
            pending.cancel("primary cancellation")
            await cleaning.wait()
            pending.cancel("repeat cancellation")
            await asyncio.sleep(0)
            with pytest.raises(SessionAdmissionError):
                reconcile_fresh(host)
            host.session.close()
            with pytest.raises(SessionStoreError, match="task_still_owned"):
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
                after = reader.inspect_task(SESSION, SPEC.task_id)
                assert (
                    after.task.result is None and after.task.diagnostic == "interrupted"
                )
                assert after.task.phase == "inspecting"
            finally:
                reader.close()
        finally:
            finish.set()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    try:
        asyncio.run(asyncio.wait_for(scenario(), 10))
    finally:
        host.session.close()


@pytest.mark.parametrize(
    "fault", ["inspection", "inspection_raise", "head_drift", "task_cas", "no_approval"]
)
def test_failed_or_changed_inspection_never_records_success(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    crash(root, "inspecting")
    llm = SequenceLLM([])
    host = make_host(root, llm, reopen=True)
    reconcile_fresh(host)
    report = host.store.inspect_task(SESSION, SPEC.task_id)
    approval = decision(report, "inspect_repository")
    receipt = report.task.checkpoint
    inspected = 0

    async def inspect(
        provider: RepoContextProvider, **kwargs: Any
    ) -> RepositoryEvidence:
        nonlocal inspected
        inspected += 1
        if fault == "inspection_raise":
            raise RuntimeError("private-sensitive-error")
        if fault == "head_drift":
            (host.workspace.root / "tracked.txt").write_text(
                "changed by concurrent host\n"
            )
            git(host.workspace.root, "add", "tracked.txt")
            git(host.workspace.root, "commit", "-m", "concurrent host")
        elif fault == "task_cas":
            current = host.store.inspect_task(SESSION, SPEC.task_id)
            host.store.require_task_recovery(current.task.revision, "interrupted")
        return RepositoryEvidence(
            workspace_root=host.workspace.root,
            inspection_error="private-sensitive-error"
            if fault == "inspection"
            else None,
        )

    monkeypatch.setattr(RepoContextProvider, "inspect_evidence", inspect)
    if fault == "no_approval":
        approval = cast(TaskResolution, None)
    try:
        with pytest.raises((SessionStoreError, SessionAdmissionError)) as error:
            asyncio.run(
                host.runner.reconcile_task(host.session, report, resolution=approval)
            )
        after = host.store.inspect_task(SESSION, SPEC.task_id)
        assert after.task.result is None and after.task.checkpoint == receipt
        assert (
            "private-sensitive-error" not in str(error.value) + after.model_dump_json()
        )
        assert inspected == int(fault != "no_approval") and llm.calls == []
        if fault != "no_approval":
            assert after.task.recovery_required
    finally:
        host.session.close()
