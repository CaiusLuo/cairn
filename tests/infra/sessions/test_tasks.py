"""Task facts are separate from committed conversation and never replay work."""

import json
import multiprocessing
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Literal, cast

import pytest

from cairn.infra.sessions.models import (
    Outcome,
    SessionStoreError,
    VersionConflict,
)
from cairn.infra.sessions.store import SessionStore
from cairn.infra.sessions.task_models import (
    TaskDiagnostic,
    TaskPhase,
    TaskRecord,
    TaskRepositoryObservation,
    TaskResultData,
)
from tests.support.sessions import OTHER_TURN, SESSION, binding, store
from tests.support.task_ledger import (
    INPUT_DIGEST,
    TASK,
    commit_task_checkpoint,
    task_crash_writer,
)

OTHER_SESSION = "00000000-0000-4000-8000-000000000004"


@pytest.fixture
def api(tmp_path: Path) -> Iterator[SessionStore]:
    value = store(tmp_path)
    value.create(binding(tmp_path))
    try:
        yield value
    finally:
        value.close()


def at_phase(api: SessionStore, phase: TaskPhase) -> TaskRecord:
    task = api.create_task(
        api.inspect(SESSION).revision, task_id=TASK, input_digest=INPUT_DIGEST
    )
    if phase != "running":
        receipt = commit_task_checkpoint(api, task, fenced=True)
        task = api.commit_task_turn(task.revision, receipt)
        if phase in ("inspecting", "result_recorded"):
            task = api.start_task_inspection(task.revision)
        if phase == "result_recorded":
            task = api.record_task_result(
                task.revision, TaskResultData(status="completed")
            )
    return task


def test_lifecycle_independent_revisions_and_historical_result(
    api: SessionStore, tmp_path: Path
) -> None:
    session = api.inspect(SESSION)
    task = at_phase(api, "running")
    assert task.revision.version == 0 and task.result is None
    assert api.inspect(SESSION) == session
    receipt = commit_task_checkpoint(api, task, fenced=True)
    # A real durable Turn alone neither advances nor completes its Task.
    report = api.inspect_task(SESSION, TASK)
    assert report.task == task and report.status == "incomplete"
    assert report.session_checkpoint == receipt
    assert report.external_effects_may_have_occurred
    task = api.commit_task_turn(task.revision, receipt)
    assert task.phase == "turn_committed" and task.revision.version == 1
    assert task.result is None
    task = api.start_task_inspection(task.revision)
    assert task.phase == "inspecting" and task.revision.version == 2
    result = TaskResultData(
        status="completed",
        trace_id="a" * 32,
        repository=TaskRepositoryObservation(
            is_git_repository=True, head_revision="b" * 40, dirty=True
        ),
    )
    task = api.record_task_result(task.revision, result)
    assert task.phase == "result_recorded" and task.revision.version == 3
    assert task.result == result and api.inspect(SESSION).receipt == receipt
    later = api.create_task(
        receipt.revision, task_id="task-2", input_digest=INPUT_DIGEST
    )
    later_receipt = commit_task_checkpoint(
        api, later, turn_id=OTHER_TURN, task_id="task-2"
    )
    report = api.inspect_task(SESSION, TASK)
    assert report.task.checkpoint == receipt
    assert report.session_checkpoint == later_receipt
    assert report.status == "result_recorded"
    api.close()
    before = {p.name: p.read_bytes() for p in api.directory.iterdir()}
    reader = store(tmp_path, mode="inspect")
    try:
        assert reader.inspect_task(SESSION, TASK) == report
        assert reader.inspect_task(SESSION, "task-2").task.result is None
    finally:
        reader.close()
    assert {p.name: p.read_bytes() for p in api.directory.iterdir()} == before


@pytest.mark.parametrize(
    ("phase", "operation"),
    [
        ("running", "inspect"),
        ("running", "result"),
        ("turn_committed", "commit"),
        ("turn_committed", "result"),
        ("inspecting", "commit"),
        ("inspecting", "inspect"),
        ("result_recorded", "commit"),
        ("result_recorded", "inspect"),
        ("result_recorded", "result"),
        ("result_recorded", "recovery"),
    ],
)
def test_invalid_transitions_leave_task_and_session_unchanged(
    api: SessionStore, phase: TaskPhase, operation: str
) -> None:
    task = at_phase(api, phase)
    session = api.inspect(SESSION)
    with pytest.raises(SessionStoreError, match="invalid_task_transition"):
        if operation == "commit":
            api.commit_task_turn(task.revision, session.receipt)
        elif operation == "inspect":
            api.start_task_inspection(task.revision)
        elif operation == "result":
            api.record_task_result(task.revision, TaskResultData(status="completed"))
        else:
            api.require_task_recovery(task.revision, "interrupted")
    assert api.inspect_task(SESSION, TASK).task == task
    assert api.inspect(SESSION).revision == session.revision


@pytest.mark.parametrize("operation", ["commit", "inspect", "result", "recovery"])
def test_stale_task_revisions_reject_every_mutation(
    api: SessionStore, operation: str
) -> None:
    phases: dict[str, TaskPhase] = {"commit": "turn_committed", "inspect": "inspecting"}
    phase = phases.get(operation, "result_recorded")
    task = at_phase(api, phase)
    stale = task.revision.model_copy(update={"version": task.revision.version - 1})
    with pytest.raises(VersionConflict):
        if operation == "commit":
            assert task.checkpoint is not None
            api.commit_task_turn(stale, task.checkpoint)
        elif operation == "inspect":
            api.start_task_inspection(stale)
        elif operation == "result":
            api.record_task_result(stale, TaskResultData(status="completed"))
        else:
            api.require_task_recovery(stale, "interrupted")
    assert api.inspect_task(SESSION, TASK).task == task


def test_task_ids_scoped_to_exact_session_and_duplicates_reject(
    api: SessionStore, tmp_path: Path
) -> None:
    task = at_phase(api, "running")
    other = api.create(binding(tmp_path, session_id=OTHER_SESSION))
    second = api.create_task(other.revision, task_id=TASK, input_digest=INPUT_DIGEST)
    assert second.revision.session_id == OTHER_SESSION
    receipt = commit_task_checkpoint(api, second)
    with pytest.raises(SessionStoreError, match="task_checkpoint_mismatch"):
        api.commit_task_turn(task.revision, receipt)
    assert api.inspect_task(SESSION, TASK).task == task
    api.close()
    reopened = store(tmp_path)
    try:
        with pytest.raises(SessionStoreError, match="task_exists"):
            reopened.create_task(
                reopened.inspect(SESSION).revision,
                task_id=TASK,
                input_digest=INPUT_DIGEST,
            )
        assert reopened.inspect_task(SESSION, TASK).task == task
        with pytest.raises(SessionStoreError, match="task_missing"):
            reopened.inspect_task(SESSION, "unknown-task")
        with pytest.raises(SessionStoreError, match="session_missing"):
            reopened.inspect_task(OTHER_TURN, TASK)
    finally:
        reopened.close()


def test_create_task_requires_current_session_revision(api: SessionStore) -> None:
    task = at_phase(api, "running")
    commit_task_checkpoint(api, task)
    with pytest.raises(VersionConflict):
        api.create_task(task.base_revision, task_id="new", input_digest=INPUT_DIGEST)
    with pytest.raises(SessionStoreError, match="task_missing"):
        api.inspect_task(SESSION, "new")


@pytest.mark.parametrize(
    "damage", ["digest", "version", "head", "turn", "outcome", "initial"]
)
def test_only_exact_real_receipt_establishes_turn_committed(
    api: SessionStore, damage: str
) -> None:
    task = at_phase(api, "running")
    initial = api.inspect(SESSION).receipt
    real = commit_task_checkpoint(api, task)
    receipt = real
    if damage == "digest":
        receipt = real.model_copy(update={"sha256": "f" * 64})
    elif damage == "version":
        receipt = real.model_copy(
            update={
                "revision": real.revision.model_copy(
                    update={"version": real.revision.version + 1}
                )
            }
        )
    elif damage == "head":
        receipt = real.model_copy(
            update={
                "revision": real.revision.model_copy(
                    update={"head_seq": real.revision.head_seq + 1}
                )
            }
        )
    elif damage == "turn":
        receipt = real.model_copy(update={"turn_id": OTHER_TURN})
    elif damage == "outcome":
        receipt = real.model_copy(update={"outcome": "cancelled"})
    else:
        receipt = initial
    with pytest.raises(SessionStoreError, match="task_checkpoint_mismatch"):
        api.commit_task_turn(task.revision, receipt)
    assert api.inspect_task(SESSION, TASK).task == task
    assert api.inspect(SESSION).receipt == real


@pytest.mark.parametrize(
    ("task_id", "user"),
    [(None, "hello"), ("other", "hello"), (TASK, "different input")],
)
def test_real_checkpoint_must_belong_to_exact_task_and_input(
    api: SessionStore, task_id: str | None, user: str
) -> None:
    task = at_phase(api, "running")
    receipt = commit_task_checkpoint(api, task, task_id=task_id, user=user)
    with pytest.raises(SessionStoreError, match="task_turn_mismatch"):
        api.commit_task_turn(task.revision, receipt)
    assert api.inspect_task(SESSION, TASK).task == task


def test_changed_session_head_blocks_further_task_phases(api: SessionStore) -> None:
    task = at_phase(api, "turn_committed")
    later = api.create_task(
        api.inspect(SESSION).revision, task_id="later", input_digest=INPUT_DIGEST
    )
    commit_task_checkpoint(api, later, turn_id=OTHER_TURN, task_id="later")
    with pytest.raises(SessionStoreError, match="task_checkpoint_not_current"):
        api.start_task_inspection(task.revision)
    assert api.inspect_task(SESSION, TASK).task == task


def test_recovery_diagnostic_blocks_advancement_without_manufacturing_result(
    api: SessionStore,
) -> None:
    task = at_phase(api, "inspecting")
    task = api.require_task_recovery(task.revision, "inspection_failed")
    assert task.revision.version == 3
    report = api.inspect_task(SESSION, TASK)
    assert report.status == "recovery_required" and report.task.result is None
    assert report.task.diagnostic == "inspection_failed"
    with pytest.raises(SessionStoreError, match="task_recovery_required"):
        api.record_task_result(task.revision, TaskResultData(status="completed"))
    assert api.inspect_task(SESSION, TASK).task == task


def test_cancelled_turn_cannot_acquire_completed_task_result(api: SessionStore) -> None:
    task = at_phase(api, "running")
    receipt = commit_task_checkpoint(api, task, outcome="cancelled")
    task = api.commit_task_turn(task.revision, receipt)
    task = api.start_task_inspection(task.revision)
    with pytest.raises(SessionStoreError, match="invalid_record"):
        api.record_task_result(task.revision, TaskResultData(status="completed"))
    assert api.inspect_task(SESSION, TASK).task == task


@pytest.mark.parametrize(
    "outcome", ["completed", "cancelled", "budget_exhausted", "runtime_error"]
)
def test_task_result_outcome_is_explicit_and_independent_of_turn(
    api: SessionStore, outcome: Outcome
) -> None:
    task = at_phase(api, "inspecting")
    result = TaskResultData(status=outcome)
    recorded = api.record_task_result(task.revision, result)
    assert recorded.result == result and recorded.phase == "result_recorded"
    assert recorded.checkpoint is not None
    assert recorded.checkpoint.outcome == "completed"


@pytest.mark.parametrize(
    "operation", ["create", "commit", "inspect", "result", "recovery"]
)
def test_read_only_task_api_rejects_mutations_without_changing_files(
    api: SessionStore, tmp_path: Path, operation: str
) -> None:
    task = at_phase(api, "inspecting")
    session = api.inspect(SESSION)
    api.close()
    before = {p.name: p.read_bytes() for p in api.directory.iterdir()}
    reader = store(tmp_path, mode="inspect")
    try:
        with pytest.raises(SessionStoreError, match="read_only_store"):
            if operation == "create":
                reader.create_task(
                    session.revision, task_id="new", input_digest=INPUT_DIGEST
                )
            elif operation == "commit":
                assert task.checkpoint is not None
                reader.commit_task_turn(task.revision, task.checkpoint)
            elif operation == "inspect":
                reader.start_task_inspection(task.revision)
            elif operation == "result":
                reader.record_task_result(
                    task.revision, TaskResultData(status="completed")
                )
            else:
                reader.require_task_recovery(task.revision, "interrupted")
        assert reader.inspect_task(SESSION, TASK).task == task
    finally:
        reader.close()
    assert {p.name: p.read_bytes() for p in api.directory.iterdir()} == before


@pytest.mark.parametrize("committed", [False, True])
def test_task_commit_uncertainty_inspects_exact_facts_without_retry(
    api: SessionStore, monkeypatch: pytest.MonkeyPatch, committed: bool
) -> None:
    task = at_phase(api, "turn_committed")

    def uncertain(conn: sqlite3.Connection) -> None:
        if committed:
            conn.commit()
        raise sqlite3.OperationalError("credential-and-provider-content")

    monkeypatch.setattr(api, "_commit", uncertain)
    with pytest.raises(SessionStoreError, match="commit_uncertain") as error:
        api.start_task_inspection(task.revision)
    assert "credential" not in str(error.value)
    report = api.inspect_task(SESSION, TASK)
    assert report.status == "recovery_required"
    assert report.task.phase == ("inspecting" if committed else "turn_committed")
    assert report.task.revision.version == task.revision.version + int(committed)
    assert report.task.result is None


def test_unexpected_interruption_rolls_back_task_update(
    api: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = at_phase(api, "turn_committed")

    def interrupt(conn: sqlite3.Connection) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(api, "_commit", interrupt)
    with pytest.raises(KeyboardInterrupt):
        api.start_task_inspection(task.revision)
    assert api.inspect_task(SESSION, TASK).task == task


@pytest.mark.parametrize(
    "value",
    ["known-sensitive-哨兵", "API_KEY=sk-test-secret-123456", "x" * 257, "bad\x1b[31m"],
)
def test_task_input_metadata_rejects_sensitive_or_unbounded_values(
    tmp_path: Path, value: str
) -> None:
    api = store(tmp_path, sensitive_values=("known-sensitive-哨兵",))
    try:
        receipt = api.create(binding(tmp_path))
        with pytest.raises(SessionStoreError) as error:
            api.create_task(receipt.revision, task_id=value, input_digest=INPUT_DIGEST)
        assert value not in str(error.value)
        api.close()
        assert all(
            value.encode() not in p.read_bytes() for p in api.directory.iterdir()
        )
    finally:
        api.close()


@pytest.mark.parametrize(
    "field", ["error", "final_response", "prompt", "trace_id", "repository"]
)
def test_results_never_serialize_untrusted_content(
    api: SessionStore, field: str
) -> None:
    task = at_phase(api, "inspecting")
    sensitive = "provider-secret-untrusted-output"
    result = TaskResultData(status="completed").model_copy(update={field: sensitive})
    with pytest.raises(SessionStoreError, match="invalid_record") as error:
        api.record_task_result(task.revision, result)
    assert sensitive not in str(error.value)
    assert api.inspect_task(SESSION, TASK).task == task
    api.close()
    assert all(
        sensitive.encode() not in p.read_bytes() for p in api.directory.iterdir()
    )


def test_valid_result_shape_still_rejects_known_secret_values(tmp_path: Path) -> None:
    sensitive = "abc" * 10 + "de"
    api = store(tmp_path, sensitive_values=(sensitive,))
    try:
        api.create(binding(tmp_path))
        task = at_phase(api, "inspecting")
        with pytest.raises(SessionStoreError, match="sensitive_record") as error:
            api.record_task_result(
                task.revision, TaskResultData(status="completed", trace_id=sensitive)
            )
        assert sensitive not in str(error.value)
        assert api.inspect_task(SESSION, TASK).task == task
        api.close()
        assert all(
            sensitive.encode() not in p.read_bytes() for p in api.directory.iterdir()
        )
    finally:
        api.close()


def test_recovery_diagnostic_never_serializes_exception_text(api: SessionStore) -> None:
    task = at_phase(api, "running")
    sensitive = "credential-containing-exception"
    with pytest.raises(SessionStoreError, match="invalid_record") as error:
        api.require_task_recovery(task.revision, cast(TaskDiagnostic, sensitive))
    assert sensitive not in str(error.value)
    assert api.inspect_task(SESSION, TASK).task == task
    api.close()
    assert all(
        sensitive.encode() not in p.read_bytes() for p in api.directory.iterdir()
    )


@pytest.mark.parametrize(
    "damage", ["revision", "phase", "checkpoint", "input", "json", "oversize", "secret"]
)
def test_task_corruption_fails_closed_without_rewriting(
    api: SessionStore, tmp_path: Path, damage: str
) -> None:
    at_phase(api, "turn_committed")
    path = api.path
    api.close()
    with sqlite3.connect(path) as conn:
        if damage == "revision":
            conn.execute("UPDATE tasks SET version=version+1")
        elif damage == "phase":
            conn.execute("UPDATE tasks SET phase='inspecting'")
        else:
            raw = conn.execute("SELECT record_json FROM tasks").fetchone()[0]
            data = json.loads(raw)
            if damage == "checkpoint":
                data["checkpoint"]["sha256"] = "c" * 64
            elif damage == "input":
                data["input_digest"] = "d" * 64
            elif damage == "secret":
                data["prompt"] = "API_KEY=sk-secret-injected"
            changed = json.dumps(
                data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            if damage == "json":
                changed = "{"
            elif damage == "oversize":
                conn.execute("PRAGMA ignore_check_constraints=ON")
                changed = "x" * 4097
            conn.execute("UPDATE tasks SET record_json=?", (changed,))
    before = {p.name: p.read_bytes() for p in api.directory.iterdir()}
    reader: SessionStore | None = None
    try:
        with pytest.raises(SessionStoreError):
            reader = store(tmp_path, mode="inspect")
            reader.inspect_task(SESSION, TASK)
    finally:
        if reader is not None:
            reader.close()
    assert {p.name: p.read_bytes() for p in api.directory.iterdir()} == before


@pytest.mark.parametrize(
    "window",
    ["running", "fenced", "checkpoint", "turn_committed", "inspecting", "update"],
)
def test_sigkill_retains_incomplete_task_and_exact_committed_session(
    tmp_path: Path,
    window: Literal[
        "running", "fenced", "checkpoint", "turn_committed", "inspecting", "update"
    ],
) -> None:
    api = store(tmp_path)
    api.create(binding(tmp_path))
    api.close()
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    worker = ctx.Process(
        target=task_crash_writer, args=(str(tmp_path), SESSION, window, child)
    )
    worker.start()
    try:
        assert parent.poll(15) and parent.recv() == window
        with pytest.raises(SessionStoreError, match="writer_busy"):
            store(tmp_path)
        worker.kill()
        worker.join(15)
        assert worker.exitcode is not None
        before = {p.name: p.read_bytes() for p in api.directory.iterdir()}
        reader = store(tmp_path, mode="inspect")
        try:
            report = reader.inspect_task(SESSION, TASK)
            assert report.status == "incomplete" and report.task.result is None
            expected_phase = (
                "running"
                if window in ("running", "fenced", "checkpoint")
                else "inspecting"
                if window == "inspecting"
                else "turn_committed"
            )
            assert report.task.phase == expected_phase
            assert report.session_checkpoint.revision.head_seq == int(
                window not in ("running", "fenced")
            )
            assert report.external_effects_may_have_occurred == (window != "running")
            if window == "fenced":
                assert reader.inspect(SESSION).status == "effects_unknown"
        finally:
            reader.close()
        assert {p.name: p.read_bytes() for p in api.directory.iterdir()} == before
        reopened = store(tmp_path)
        try:
            assert reopened.inspect_task(SESSION, TASK) == report
            assert reopened.inspect(SESSION).receipt == report.session_checkpoint
        finally:
            reopened.close()
        effect_path = tmp_path / "work" / "effect.log"
        assert effect_path.exists() == (window != "running")
        if effect_path.exists():
            assert effect_path.read_bytes() == b"effect\n"
    finally:
        if worker.is_alive():
            worker.kill()
        worker.join(15)
        parent.close()
        child.close()
