"""Offline legacy RuntimeCatalogue migration acceptance."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from cairn.core.domain import ConversationSource, TaskStatus
from cairn.infra import domain_migration
from cairn.infra.domain_store import DomainStore
from cairn.infra.runtime_catalog import RuntimeEvent, RuntimeTask
from cairn.infra.sessions.models import CheckpointReceipt, Revision
from cairn.infra.task_board.store import TaskBoardStore


def _receipt() -> CheckpointReceipt:
    return CheckpointReceipt(
        revision=Revision(session_id=str(uuid4()), version=1, head_seq=0),
        turn_id=None,
        sha256="a" * 64,
        outcome=None,
    )


def _legacy_files(tmp_path: Path) -> tuple[Path, Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    tasks = {
        "task-chat": RuntimeTask(
            task_id="task-chat",
            prompt="hello",
            source="cli",
            mode="conversation",
            conversation_key="conversation-key",
            status="completed",
            session_id="session-chat",
            sequence=2,
            attempts=1,
            metrics={"steps": 1},
        ).model_dump(mode="json"),
        "task-dev": RuntimeTask(
            task_id="task-dev",
            prompt="build it",
            source="cli",
            mode="development",
            conversation_key="conversation-key",
            development_prompt="build the feature",
            worktree="/tmp/retained-worktree",
            branch="cairn/runtime/task-dev",
            status="running",
            session_id="session-dev",
            history_origins=(_receipt(),),
            sequence=2,
            attempts=3,
        ).model_dump(mode="json"),
        "task-other": RuntimeTask(
            task_id="task-other",
            prompt="other goal",
            source="web",
            mode="development",
            status="completed",
            sequence=1,
        ).model_dump(mode="json"),
    }
    events = {
        key: [
            RuntimeEvent(task_id=key, sequence=1, kind="accepted", data={}).model_dump(
                mode="json"
            ),
            RuntimeEvent(task_id=key, sequence=2, kind="completed", data={}).model_dump(
                mode="json"
            ),
        ][: task["sequence"]]
        for key, task in tasks.items()
    }
    (runtime / "runtime.json").write_text(
        json.dumps({"version": 1, "tasks": tasks, "events": events}, indent=2) + "\n"
    )
    board = TaskBoardStore(tmp_path / "board")
    board.create(title="Manual board card", description="keep me")
    return runtime, workspace, board.path


def _plan(tmp_path: Path) -> domain_migration.MigrationPlan:
    runtime, workspace, board_path = _legacy_files(tmp_path)
    return domain_migration.plan_migration(
        runtime, workspace, taskboard_path=board_path
    )


def test_plan_classifies_chat_development_and_conversation_to_development(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    assert len(plan.conversations) == 3
    assert len(plan.tasks) == 3
    assert len(plan.runs) == 3
    assert "Manual board card" in {task.title for task in plan.tasks}
    assert not plan.issues
    assert not any(task.task_id == "task-chat" for task in plan.tasks)
    dev = next(task for task in plan.tasks if task.task_id == "task-dev")
    assert dev.conversation_id == next(
        conversation.conversation_id
        for conversation in plan.conversations
        if conversation.metadata["legacy_conversation_key"] == "conversation-key"
    )
    assert dev.worktree == "/tmp/retained-worktree"
    run = next(run for run in plan.runs if run.task_id == "task-dev")
    assert run.session_id == "session-dev"
    assert run.checkpoint_receipt is not None
    assert run.metadata["legacy_attempts"] == 3
    assert [event["sequence"] for event in run.metadata["legacy_events"]] == [1, 2]
    other = next(task for task in plan.tasks if task.task_id == "task-other")
    assert other.status is TaskStatus.TODO


def test_dry_run_writes_nothing_and_apply_is_atomic_and_idempotent(
    tmp_path: Path,
) -> None:
    runtime, workspace, board_path = _legacy_files(tmp_path)
    target = tmp_path / "domain.sqlite3"
    backup_root = tmp_path / "backups"
    dry = domain_migration.migrate_domain(
        runtime,
        workspace,
        target=target,
        backup_root=backup_root,
        taskboard_path=board_path,
        dry_run=True,
    )
    assert dry.dry_run and not target.exists() and not backup_root.exists()
    source_before = (runtime / "runtime.json").read_bytes()
    board_before = board_path.read_bytes()
    applied = domain_migration.migrate_domain(
        runtime,
        workspace,
        target=target,
        backup_root=backup_root,
        taskboard_path=board_path,
    )
    assert not applied.already_migrated
    assert target.is_file()
    assert (
        applied.backup is not None
        and (Path(applied.backup) / "manifest.json").is_file()
    )
    assert (runtime / "runtime.json").read_bytes() == source_before
    assert board_path.read_bytes() == board_before
    store = DomainStore(target)
    assert store.counts() == {"conversations": 3, "tasks": 3, "runs": 3}
    restored = store.get_task("task-other")
    assert restored is not None and restored.status is TaskStatus.TODO
    repeated = domain_migration.migrate_domain(
        runtime,
        workspace,
        target=target,
        backup_root=backup_root,
        taskboard_path=board_path,
    )
    assert repeated.already_migrated
    assert store.counts() == {"conversations": 3, "tasks": 3, "runs": 3}


def test_ambiguous_legacy_record_is_reported_without_deletion(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ambiguous = RuntimeTask(
        task_id="task-ambiguous",
        prompt="ambiguous",
        source="cli",
        mode="conversation",
        worktree="/tmp/not-a-chat-worktree",
        status="completed",
    )
    (runtime / "runtime.json").write_text(
        json.dumps(
            {
                "version": 1,
                "tasks": {ambiguous.task_id: ambiguous.model_dump(mode="json")},
                "events": {ambiguous.task_id: []},
            }
        )
    )
    plan = domain_migration.plan_migration(runtime, workspace)
    assert [issue.kind for issue in plan.issues] == [
        "ambiguous_conversation_development"
    ]
    assert (runtime / "runtime.json").is_file()


def test_migration_failure_keeps_source_and_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, workspace, board_path = _legacy_files(tmp_path)
    target = tmp_path / "domain.sqlite3"
    source_before = (runtime / "runtime.json").read_bytes()
    real_populate = domain_migration._populate

    def fail(store: DomainStore, plan: Any) -> None:
        del plan
        store.create_conversation(
            workspace=workspace,
            source=ConversationSource.CLI,
            initial_prompt="partial",
        )
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(domain_migration, "_populate", fail)
    with pytest.raises(RuntimeError, match="injected"):
        domain_migration.migrate_domain(
            runtime,
            workspace,
            target=target,
            backup_root=tmp_path / "backups",
            taskboard_path=board_path,
        )
    assert not target.exists()
    assert (runtime / "runtime.json").read_bytes() == source_before
    backup = next((tmp_path / "backups").iterdir())
    assert (backup / "manifest.json").is_file()
    assert (backup / "runtime.json").is_file()
    monkeypatch.setattr(domain_migration, "_populate", real_populate)
