"""Offline RuntimeCatalogue v1 -> Conversation/Task/Run migration.

The legacy catalogue is read as input and never rewritten.  The migration
produces a new SQLite domain store in a sibling temporary file and atomically
publishes it only after validation.  A private copy of the legacy files is
created before publication so a failed migration leaves both originals and a
usable backup.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from cairn.core.domain import (
    Conversation,
    ConversationSource,
    Run,
    RunStatus,
    Task,
    TaskStatus,
    utc_now,
    workspace_id_for,
)
from cairn.core.task_board import TaskBoardItem, TaskBoardStatus
from cairn.infra.domain_store import DomainStore
from cairn.infra.runtime_catalog import CatalogueData, RuntimeEvent, RuntimeTask

_MIGRATION_VERSION = 1
_RUN_STATUS = {
    "queued": RunStatus.QUEUED,
    "running": RunStatus.RUNNING,
    "paused": RunStatus.PAUSED,
    "completed": RunStatus.COMPLETED,
    "failed": RunStatus.FAILED,
    "cancelled": RunStatus.CANCELLED,
    "recovery_required": RunStatus.RECOVERY_REQUIRED,
}


class MigrationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    legacy_task_id: str
    kind: str
    detail: str


class MigrationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    dry_run: bool
    source: str
    target: str
    backup: str | None = None
    conversations: int
    tasks: int
    runs: int
    issues: tuple[MigrationIssue, ...] = ()
    already_migrated: bool = False
    taskboard_preserved: bool = False
    source_sha256: str


@dataclass(frozen=True)
class MigrationPlan:
    runtime_root: Path
    workspace: Path
    taskboard_path: Path | None
    conversations: tuple[Conversation, ...]
    tasks: tuple[Task, ...]
    runs: tuple[Run, ...]
    issues: tuple[MigrationIssue, ...]
    source_sha256: str
    board_tasks: tuple[Task, ...] = ()
    taskboard_sha256: str | None = None
    taskboard_bytes: bytes | None = field(default=None, repr=False)

    def report(
        self, *, dry_run: bool, target: Path, backup: Path | None = None
    ) -> MigrationReport:
        return MigrationReport(
            dry_run=dry_run,
            source=str(self.runtime_root / "runtime.json"),
            target=str(target),
            backup=None if backup is None else str(backup),
            conversations=len(self.conversations),
            tasks=len(self.tasks),
            runs=len(self.runs),
            issues=self.issues,
            taskboard_preserved=self.taskboard_bytes is not None,
            source_sha256=self.source_sha256,
        )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _derived(prefix: str, value: str) -> str:
    return prefix + hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _conversation_id(workspace_id: str, key: str) -> str:
    return _derived("conv-", f"{workspace_id}:{key}")


def _run_id(legacy_task_id: str) -> str:
    return _derived("run-", legacy_task_id)


def _conversation_source(source: str) -> ConversationSource:
    return ConversationSource(source)


def _task_status(legacy: RuntimeTask) -> TaskStatus:
    # This is the new Task's *initial* state.  A legacy RuntimeTask completed
    # status is a Run fact and must never be translated into Task.DONE/CLOSED.
    if legacy.status == "running":
        return TaskStatus.IN_PROGRESS
    if legacy.status in {"paused", "recovery_required"}:
        return TaskStatus.WAITING
    return TaskStatus.TODO


def _run_status(legacy: RuntimeTask, issue: MigrationIssue | None = None) -> RunStatus:
    del issue
    return _RUN_STATUS.get(legacy.status, RunStatus.FAILED)


_BOARD_TASK_STATUS = {
    TaskBoardStatus.TODO: TaskStatus.TODO,
    TaskBoardStatus.QUEUED: TaskStatus.TODO,
    TaskBoardStatus.RUNNING: TaskStatus.IN_PROGRESS,
    TaskBoardStatus.WAITING: TaskStatus.WAITING,
    TaskBoardStatus.COMPLETED: TaskStatus.DONE,
    TaskBoardStatus.FAILED: TaskStatus.CLOSED,
    TaskBoardStatus.CANCELLED: TaskStatus.CLOSED,
}


def _board_task(
    item: TaskBoardItem, *, workspace_id: str, conversation_id: str
) -> Task:
    return Task(
        task_id=item.task_id,
        conversation_id=conversation_id,
        workspace_id=workspace_id,
        title=item.title,
        goal=item.description or item.title,
        status=_BOARD_TASK_STATUS[item.status],
        issue_url=item.issue_url,
        worktree=None,
        branch=None,
        created_at=item.created_at,
        updated_at=item.updated_at,
        metadata={
            "legacy_taskboard": item.model_dump(mode="json"),
            "legacy_board_task": True,
        },
    )


def _legacy_metadata(task: RuntimeTask, events: list[RuntimeEvent]) -> dict[str, Any]:
    return {
        "legacy_task_id": task.task_id,
        "legacy_task": task.model_dump(mode="json"),
        "legacy_events": [event.model_dump(mode="json") for event in events],
        "legacy_attempts": task.attempts,
        "legacy_status": task.status,
        "legacy_sequence": task.sequence,
    }


def _task_metadata(task: RuntimeTask) -> dict[str, Any]:
    return {
        "legacy_task_id": task.task_id,
        "legacy_status": task.status,
        "legacy_mode": task.mode,
        "legacy_conversation_key": task.conversation_key,
        "legacy_request_id": task.request_id,
        "legacy_session_id": task.session_id,
        "legacy_history_origins": [
            receipt.model_dump(mode="json") for receipt in task.history_origins
        ],
        "legacy_input_receipts": dict(task.input_receipts),
    }


def _build_plan(
    *,
    runtime_root: Path,
    workspace: Path,
    taskboard_path: Path | None,
) -> MigrationPlan:
    runtime_file = runtime_root / "runtime.json"
    if runtime_file.is_symlink() or not runtime_file.is_file():
        raise ValueError("RuntimeCatalogue v1 runtime.json is missing or a symlink")
    data = CatalogueData.model_validate_json(runtime_file.read_bytes())
    workspace_id = workspace_id_for(workspace)
    conversations: dict[str, Conversation] = {}
    tasks: list[Task] = []
    runs: list[Run] = []
    issues: list[MigrationIssue] = []
    for legacy_id, task in data.tasks.items():
        if legacy_id != task.task_id:
            issues.append(
                MigrationIssue(
                    legacy_task_id=legacy_id,
                    kind="identity_mismatch",
                    detail="Catalogue key differs from RuntimeTask.task_id; preserved only in source",
                )
            )
            continue
        event_sequences = [event.sequence for event in data.events.get(legacy_id, [])]
        if event_sequences != list(range(1, task.sequence + 1)):
            issues.append(
                MigrationIssue(
                    legacy_task_id=legacy_id,
                    kind="invalid_event_history",
                    detail="Legacy event sequence is not contiguous; preserved only in source",
                )
            )
            continue
        if task.mode == "conversation" and (
            task.worktree is not None or task.development_prompt is not None
        ):
            issues.append(
                MigrationIssue(
                    legacy_task_id=task.task_id,
                    kind="ambiguous_conversation_development",
                    detail="Conversation-mode record has development fields; source preserved",
                )
            )
            continue
        if task.mode not in {"conversation", "development"}:
            issues.append(
                MigrationIssue(
                    legacy_task_id=task.task_id,
                    kind="unknown_mode",
                    detail=str(task.mode),
                )
            )
            continue
        key = task.conversation_key or task.task_id
        conversation_id = _conversation_id(workspace_id, key)
        if conversation_id not in conversations:
            conversations[conversation_id] = Conversation(
                conversation_id=conversation_id,
                workspace_id=workspace_id,
                source=_conversation_source(task.source),
                initial_prompt=task.prompt,
                created_at=utc_now(),
                updated_at=utc_now(),
                metadata={
                    "legacy_conversation_key": task.conversation_key,
                    "entrypoint": task.source,
                    "legacy_task_ids": [task.task_id],
                },
            )
        else:
            prior = conversations[conversation_id]
            conversations[conversation_id] = prior.model_copy(
                update={
                    "updated_at": utc_now(),
                    "metadata": {
                        **prior.metadata,
                        "legacy_task_ids": [
                            *prior.metadata.get("legacy_task_ids", []),
                            task.task_id,
                        ],
                    },
                }
            )
        development = task.mode == "development"
        run = Run(
            run_id=_run_id(task.task_id),
            conversation_id=conversation_id,
            task_id=task.task_id if development else None,
            workspace_id=workspace_id,
            status=_run_status(task),
            error=task.diagnostic,
            budget=task.metrics,
            trace_ref=None,
            session_id=task.session_id,
            checkpoint_receipt=(
                task.history_origins[-1].model_dump(mode="json")
                if task.history_origins
                else None
            ),
            created_at=utc_now(),
            updated_at=utc_now(),
            ended_at=utc_now()
            if task.status in {"completed", "failed", "cancelled"}
            else None,
            metadata=_legacy_metadata(task, data.events.get(task.task_id, [])),
        )
        runs.append(run)
        if development:
            tasks.append(
                Task(
                    task_id=task.task_id,
                    conversation_id=conversation_id,
                    workspace_id=workspace_id,
                    title=task.prompt[:300],
                    goal=task.development_prompt or task.prompt,
                    status=_task_status(task),
                    issue_url=None,
                    worktree=task.worktree,
                    branch=task.branch,
                    created_at=utc_now(),
                    updated_at=utc_now(),
                    metadata=_task_metadata(task),
                )
            )
    taskboard_bytes = None
    taskboard_sha = None
    board_tasks: list[Task] = []
    if taskboard_path is not None and taskboard_path.exists():
        if taskboard_path.is_symlink():
            raise ValueError("TaskBoard source cannot be a symlink")
        taskboard_bytes = taskboard_path.read_bytes()
        taskboard_sha = hashlib.sha256(taskboard_bytes).hexdigest()
        try:
            board_data = json.loads(taskboard_bytes)
            raw_items = board_data.get("tasks", [])
            if board_data.get("version") != 1 or not isinstance(raw_items, list):
                raise ValueError("unsupported TaskBoard shape")
            board_items = [TaskBoardItem.model_validate(item) for item in raw_items]
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            issues.append(
                MigrationIssue(
                    legacy_task_id="",
                    kind="taskboard_unreadable",
                    detail=type(error).__name__,
                )
            )
        else:
            board_conversations: dict[str, str] = {}
            for item in board_items:
                key = item.project or "legacy-taskboard"
                if key not in board_conversations:
                    conversation_id = _conversation_id(workspace_id, "board:" + key)
                    board_conversations[key] = conversation_id
                    if conversation_id not in conversations:
                        conversations[conversation_id] = Conversation(
                            conversation_id=conversation_id,
                            workspace_id=workspace_id,
                            source=ConversationSource.CLI,
                            initial_prompt="Imported TaskBoard: " + key,
                            created_at=item.created_at,
                            updated_at=item.updated_at,
                            metadata={
                                "legacy_taskboard_project": item.project,
                                "imported_from": "tasks.json",
                            },
                        )
                if item.task_id in {task.task_id for task in tasks}:
                    issues.append(
                        MigrationIssue(
                            legacy_task_id=item.task_id,
                            kind="taskboard_identity_collision",
                            detail="TaskBoard item collides with a RuntimeTask id; board file preserved",
                        )
                    )
                    continue
                board_tasks.append(
                    _board_task(
                        item,
                        workspace_id=workspace_id,
                        conversation_id=board_conversations[key],
                    )
                )
    return MigrationPlan(
        runtime_root=runtime_root,
        workspace=workspace,
        taskboard_path=taskboard_path,
        conversations=tuple(conversations.values()),
        tasks=tuple((*tasks, *board_tasks)),
        runs=tuple(runs),
        board_tasks=tuple(board_tasks),
        issues=tuple(issues),
        source_sha256=_sha(runtime_file),
        taskboard_sha256=taskboard_sha,
        taskboard_bytes=taskboard_bytes,
    )


def plan_migration(
    runtime_root: Path,
    workspace: Path,
    *,
    taskboard_path: Path | None = None,
) -> MigrationPlan:
    """Read and classify legacy data without writing anything."""
    return _build_plan(
        runtime_root=runtime_root.expanduser().resolve(),
        workspace=workspace.expanduser().resolve(),
        taskboard_path=None
        if taskboard_path is None
        else taskboard_path.expanduser().resolve(),
    )


def _backup(plan: MigrationPlan, backup_root: Path) -> Path:
    backup = backup_root / (
        "domain-v1-" + plan.source_sha256[:12] + "-" + uuid4().hex[:8]
    )
    backup.mkdir(parents=True, mode=0o700)
    files = [("runtime.json", plan.runtime_root / "runtime.json")]
    if plan.taskboard_bytes is not None and plan.taskboard_path is not None:
        files.append(("tasks.json", plan.taskboard_path))
    manifest: dict[str, str] = {}
    for name, source in files:
        destination = backup / name
        shutil.copy2(source, destination)
        destination.chmod(0o600)
        manifest[name] = hashlib.sha256(destination.read_bytes()).hexdigest()
    (backup / "manifest.json").write_text(
        json.dumps(
            {
                "version": _MIGRATION_VERSION,
                "source": str(plan.runtime_root),
                "source_sha256": plan.source_sha256,
                "taskboard_sha256": plan.taskboard_sha256,
                "files": manifest,
            },
            indent=2,
        )
        + "\n"
    )
    (backup / "manifest.json").chmod(0o600)
    return backup


def _populate(store: DomainStore, plan: MigrationPlan) -> None:
    for conversation in plan.conversations:
        store.create_conversation(
            workspace=plan.workspace,
            source=conversation.source,
            initial_prompt=conversation.initial_prompt,
            conversation_id=conversation.conversation_id,
            created_at=conversation.created_at,
            metadata=conversation.metadata,
        )
    for task in plan.tasks:
        store.create_task(
            conversation_id=task.conversation_id,
            goal=task.goal,
            title=task.title,
            status=task.status,
            task_id=task.task_id,
            issue_url=task.issue_url,
            worktree=task.worktree,
            branch=task.branch,
            created_at=task.created_at,
            metadata=task.metadata,
        )
    for run in plan.runs:
        store.create_run(
            conversation_id=run.conversation_id,
            task_id=run.task_id,
            run_id=run.run_id,
            status=run.status,
            error=run.error,
            budget=run.budget,
            trace_ref=run.trace_ref,
            session_id=run.session_id,
            checkpoint_receipt=run.checkpoint_receipt,
            created_at=run.created_at,
            metadata=run.metadata,
        )
    store.mark_migration(
        source_sha256=plan.source_sha256, source=str(plan.runtime_root)
    )


def apply_migration(
    plan: MigrationPlan,
    *,
    target: Path,
    backup_root: Path,
) -> MigrationReport:
    """Publish a verified domain store; source and existing targets never mutate."""
    target = target.expanduser().resolve()
    backup_root = backup_root.expanduser().resolve()
    if target.exists():
        existing = DomainStore(target)
        if existing.has_migration(plan.source_sha256):
            return plan.report(dry_run=False, target=target).model_copy(
                update={"already_migrated": True}
            )
        if any(existing.counts().values()):
            raise ValueError("Domain store target is not an empty migration target")
    backup = _backup(plan, backup_root)
    temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        store = DomainStore(temporary)
        _populate(store, plan)
        counts = store.counts()
        if counts != {
            "conversations": len(plan.conversations),
            "tasks": len(plan.tasks),
            "runs": len(plan.runs),
        }:
            raise ValueError("Domain migration verification failed")
        os.replace(temporary, target)
        target.chmod(0o600)
    except BaseException:
        if temporary.exists():
            temporary.unlink()
        raise
    return plan.report(dry_run=False, target=target, backup=backup)


def migrate_domain(
    runtime_root: Path,
    workspace: Path,
    *,
    target: Path,
    backup_root: Path,
    taskboard_path: Path | None = None,
    dry_run: bool = False,
) -> MigrationReport:
    plan = plan_migration(runtime_root, workspace, taskboard_path=taskboard_path)
    if dry_run:
        return plan.report(dry_run=True, target=target)
    return apply_migration(plan, target=target, backup_root=backup_root)


__all__ = [
    "MigrationIssue",
    "MigrationPlan",
    "MigrationReport",
    "apply_migration",
    "migrate_domain",
    "plan_migration",
]
