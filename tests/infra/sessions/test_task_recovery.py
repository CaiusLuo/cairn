"""Host decisions preserve exact Task/Session facts and never dispatch work."""

import sqlite3
from pathlib import Path

import pytest

from cairn.infra.sessions.models import (
    HostResolution,
    SessionStoreError,
    VersionConflict,
)
from cairn.infra.sessions.task_models import TaskResolution, TaskResultData
from tests.support.sessions import SESSION, T4, binding, fence, intent, store
from tests.support.task_ledger import INPUT_DIGEST, TASK, commit_task_checkpoint


def test_abandonment_preserves_unknown_effect_evidence_and_blocks_advancement(
    tmp_path: Path,
) -> None:
    api = store(tmp_path)
    api.create(binding(tmp_path))
    task = api.create_task(
        api.inspect(SESSION).revision, task_id=TASK, input_digest=INPUT_DIGEST
    )
    assert "resolution" not in task.model_dump()
    with sqlite3.connect(api.path) as conn:
        assert (
            '"resolution"'
            not in conn.execute("SELECT record_json FROM tasks").fetchone()[0]
        )
    turn = intent(binding(tmp_path), task.base_revision).model_copy(
        update={"task_id": TASK}
    )
    active = api.begin_turn(task.base_revision, turn)
    active = api.fence_tool(active, fence(binding(tmp_path)))
    # A live owner cannot reconcile, even before any result is known.
    with (
        pytest.raises(SessionStoreError),
        api.task_reconciliation(api.inspect_task(SESSION, TASK)),
    ):
        pytest.fail("Live work acquired recovery ownership")
    # Settle local ownership as the existing crash-release contract requires.
    active = api.require_recovery(active, "storage_failed")
    api.release_turn(active, turn.turn_id)
    session = api.inspect(SESSION)
    api.reconcile(
        session.revision,
        HostResolution(
            session_id=SESSION,
            turn_id=turn.turn_id,
            actor="host",
            resolved_at_utc=T4,
            binding=session.binding,
        ),
    )
    before = api.inspect(SESSION)
    report = api.inspect_task(SESSION, TASK)
    decision = TaskResolution(
        task_revision=report.task.revision,
        session_revision=report.session_revision,
        input_digest=INPUT_DIGEST,
        action="abandon_without_replay",
        actor="host",
        resolved_at_utc=T4,
    )
    with api.task_reconciliation(report):
        abandoned = api.reconcile_task(report, decision)
    saved = api.inspect_task(SESSION, TASK)
    assert saved.status == "abandoned" and saved.external_effects_may_have_occurred
    assert (
        saved.task == abandoned
        and abandoned.result is None
        and abandoned.checkpoint is None
    )
    assert abandoned.recovery_required and abandoned.resolution == decision
    assert api.inspect(SESSION) == before
    with pytest.raises(SessionStoreError):
        api.commit_task_turn(abandoned.revision, report.session_checkpoint)
    api.close()
    reader = store(tmp_path, mode="inspect")
    try:
        assert reader.inspect_task(SESSION, TASK).status == "abandoned"
    finally:
        reader.close()


@pytest.mark.parametrize(
    "mismatch", ["task_cas", "session_cas", "input", "actor", "time"]
)
def test_recovery_approval_is_bound_to_exact_revisions_and_input(
    tmp_path: Path,
    mismatch: str,
) -> None:
    api = store(tmp_path)
    api.create(binding(tmp_path))
    task = api.create_task(
        api.inspect(SESSION).revision, task_id=TASK, input_digest=INPUT_DIGEST
    )
    receipt = commit_task_checkpoint(api, task, fenced=True)
    report = api.inspect_task(SESSION, TASK)
    decision = TaskResolution(
        task_revision=task.revision,
        session_revision=report.session_revision,
        input_digest=INPUT_DIGEST,
        action="acknowledge_checkpoint",
        actor="host",
        resolved_at_utc=T4,
    )
    if mismatch == "task_cas":
        api.require_task_recovery(task.revision, "interrupted")
    elif mismatch == "session_cas":
        api.require_recovery(report.session_revision, "storage_failed")
    elif mismatch == "input":
        decision = decision.model_copy(update={"input_digest": "0" * 64})
    elif mismatch == "actor":
        decision = decision.model_copy(update={"actor": ""})
    else:
        decision = decision.model_copy(
            update={"resolved_at_utc": "2019-01-01T00:00:00Z"}
        )
    before = api.inspect_task(SESSION, TASK)
    with (
        pytest.raises((SessionStoreError, VersionConflict)),
        api.task_reconciliation(report),
    ):
        api.reconcile_task(report, decision)
    assert api.inspect_task(SESSION, TASK).task == before.task
    assert api.inspect(SESSION).receipt == receipt
    api.close()


def test_acknowledgement_requires_real_attributed_receipt_and_no_borrowed_ownership(
    tmp_path: Path,
) -> None:
    api = store(tmp_path)
    api.create(binding(tmp_path))
    task = api.create_task(
        api.inspect(SESSION).revision, task_id=TASK, input_digest=INPUT_DIGEST
    )
    initial = api.inspect_task(SESSION, TASK)
    with (
        api.task_execution(task.revision),
        pytest.raises(SessionStoreError),
        api.task_reconciliation(initial),
    ):
        pytest.fail("Execution ownership was borrowed")
    commit_task_checkpoint(api, task, task_id="other")
    report = api.inspect_task(SESSION, TASK)
    decision = TaskResolution(
        task_revision=task.revision,
        session_revision=report.session_revision,
        input_digest=INPUT_DIGEST,
        action="acknowledge_checkpoint",
        actor="host",
        resolved_at_utc=T4,
    )
    with api.task_reconciliation(report), pytest.raises(SessionStoreError):
        api.reconcile_task(report, decision)
    assert api.inspect_task(SESSION, TASK).task == task
    api.close()


def test_recovery_does_not_complete_a_cancelled_turn_as_success(tmp_path: Path) -> None:
    api = store(tmp_path)
    api.create(binding(tmp_path))
    task = api.create_task(
        api.inspect(SESSION).revision, task_id=TASK, input_digest=INPUT_DIGEST
    )
    commit_task_checkpoint(api, task, outcome="cancelled")
    for action in ("acknowledge_checkpoint", "inspect_repository"):
        report = api.inspect_task(SESSION, TASK)
        decision = TaskResolution(
            task_revision=report.task.revision,
            session_revision=report.session_revision,
            input_digest=INPUT_DIGEST,
            action=action,
            actor="host",
            resolved_at_utc=T4,
        )
        with api.task_reconciliation(report):
            task = api.reconcile_task(report, decision)
    with pytest.raises(SessionStoreError):
        api.record_task_result(task.revision, TaskResultData(status="completed"))
    assert api.inspect_task(SESSION, TASK).task.result is None
    api.close()


@pytest.mark.parametrize(
    ("committed", "action"),
    [
        (False, "acknowledge_checkpoint"),
        (False, "inspect_repository"),
        (True, "abandon_without_replay"),
        (True, "inspect_repository"),  # The Task must acknowledge its receipt first.
    ],
)
def test_decision_cannot_invent_a_receipt_discard_one_or_skip_acknowledgement(
    tmp_path: Path, committed: bool, action: str
) -> None:
    api = store(tmp_path)
    api.create(binding(tmp_path))
    task = api.create_task(
        api.inspect(SESSION).revision, task_id=TASK, input_digest=INPUT_DIGEST
    )
    if committed:
        commit_task_checkpoint(api, task, fenced=True)
    report = api.inspect_task(SESSION, TASK)
    resolution = TaskResolution.model_validate(
        {
            "task_revision": task.revision,
            "session_revision": report.session_revision,
            "input_digest": INPUT_DIGEST,
            "action": action,
            "actor": "host",
            "resolved_at_utc": T4,
        }
    )
    with api.task_reconciliation(report), pytest.raises(SessionStoreError):
        api.reconcile_task(report, resolution)
    assert api.inspect_task(SESSION, TASK).task == task
    api.close()
