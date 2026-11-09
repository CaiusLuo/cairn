"""Offline RuntimeCatalogue v1 -> Conversation/Task/Run migration.

The legacy catalogue is read as input and never rewritten.  The migration
produces a new SQLite domain store in a sibling temporary file and atomically
publishes it only after validation.  A private copy of the legacy files is
created before publication so a failed migration leaves both originals and a
usable backup.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from cairn.conversation.models import (
    Conversation,
    ConversationSource,
    Run,
    RunStatus,
    utc_now,
    workspace_id_for,
)
from cairn.conversation.store import DomainStore
from cairn.development.board.models import TaskBoardItem, TaskBoardStatus
from cairn.development.catalogue import CatalogueData, RuntimeEvent, RuntimeTask
from cairn.development.models import Task, TaskStatus

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
    runtime_bytes: bytes = field(default=b"", repr=False)
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


def _lock_held(path: Path) -> bool:
    if not path.exists():
        return False
    with path.open("rb") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return False


def _assert_runtime_offline(runtime_root: Path) -> None:
    locks = (
        runtime_root / "owner.lock",
        runtime_root / "server.lock",
        runtime_root.parent / "server.lock",
        runtime_root / "sessions/writer.lock",
    )
    for path in locks:
        if _lock_held(path):
            raise RuntimeError(
                f"Legacy Runtime is online or has an active writer lock: {path}; stop it before migration"
            )


def discover_taskboard(runtime_root: Path) -> Path | None:
    """Find the configured legacy TaskBoard without guessing between candidates."""
    candidates = (
        runtime_root / "board" / "tasks.json",
        runtime_root.parent / "tasks" / "tasks.json",
        runtime_root.parent / "board" / "tasks.json",
    )
    existing = [path for path in candidates if path.is_file()]
    if len(existing) > 1:
        raise ValueError(
            "Multiple TaskBoard sources found; pass an explicit --taskboard path"
        )
    return existing[0] if existing else None


def _conversation_source(source: str) -> ConversationSource:
    return ConversationSource(source)


def _development_evidence(legacy: RuntimeTask) -> bool:
    """Recognize the real chat->development handoff without guessing."""
    if legacy.mode == "development":
        return True
    if legacy.development_prompt is not None:
        return True
    # Worktree admission is paired with a fresh session in the real handoff.
    return legacy.worktree is not None and legacy.session_id is not None


def _task_status(legacy: RuntimeTask) -> TaskStatus:
    # This is the new Task's *initial* state.  A legacy RuntimeTask completed
    # status is a Run fact and must never be translated into Task.DONE/CLOSED.
    if legacy.status in {"running", "paused", "recovery_required"}:
        return TaskStatus.WAITING
    return TaskStatus.TODO


def _run_status(legacy: RuntimeTask, issue: MigrationIssue | None = None) -> RunStatus:
    del issue
    if legacy.status in {"running", "recovery_required"}:
        return RunStatus.RECOVERY_REQUIRED
    return _RUN_STATUS.get(legacy.status, RunStatus.FAILED)


_BOARD_TASK_STATUS = {
    TaskBoardStatus.TODO: TaskStatus.TODO,
    TaskBoardStatus.QUEUED: TaskStatus.TODO,
    TaskBoardStatus.RUNNING: TaskStatus.IN_PROGRESS,
    TaskBoardStatus.WAITING: TaskStatus.WAITING,
    TaskBoardStatus.COMPLETED: TaskStatus.DONE,
    TaskBoardStatus.FAILED: TaskStatus.WAITING,
    TaskBoardStatus.CANCELLED: TaskStatus.WAITING,
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
        "legacy_development_prompt": task.development_prompt,
        "legacy_worktree": task.worktree,
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
    runtime_bytes = runtime_file.read_bytes()
    data = CatalogueData.model_validate_json(runtime_bytes)
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
        development = _development_evidence(task)
        if task.mode not in {"conversation", "development"}:
            issues.append(
                MigrationIssue(
                    legacy_task_id=task.task_id,
                    kind="unknown_mode",
                    detail=str(task.mode),
                )
            )
            continue
        if (
            task.mode == "conversation"
            and task.worktree is not None
            and task.session_id is None
            and task.development_prompt is None
        ):
            issues.append(
                MigrationIssue(
                    legacy_task_id=task.task_id,
                    kind="ambiguous_conversation_development",
                    detail="Worktree evidence has no session or development prompt; source preserved",
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
        run = Run(
            run_id=_run_id(task.task_id),
            conversation_id=conversation_id,
            task_id=task.task_id if development else None,
            workspace_id=workspace_id,
            status=_run_status(task),
            prompt=task.prompt,
            request_id=task.request_id,
            source=ConversationSource(task.source),
            mode="conversation" if not development else "development",
            result=task.result,
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
            runtime_tasks = {task.task_id: task for task in tasks}
            for item in board_items:
                runtime_task = runtime_tasks.get(item.task_id)
                if runtime_task is not None:
                    # Same identity: merge board-owned user metadata into the
                    # runtime Task; never create a second Task or a board-only
                    # Conversation for it.
                    index = next(
                        (
                            idx
                            for idx, task in enumerate(tasks)
                            if task.task_id == item.task_id
                        ),
                        None,
                    )
                    if index is None:
                        issues.append(
                            MigrationIssue(
                                legacy_task_id=item.task_id,
                                kind="taskboard_identity_without_task",
                                detail="TaskBoard card matches a conversation-only RuntimeTask; source preserved",
                            )
                        )
                        continue
                    domain_task = tasks[index]
                    tasks[index] = domain_task.model_copy(
                        update={
                            "title": item.title or domain_task.title,
                            "issue_url": item.issue_url
                            if item.issue_url is not None
                            else domain_task.issue_url,
                            "metadata": {
                                **domain_task.metadata,
                                "legacy_taskboard": item.model_dump(mode="json"),
                            },
                        }
                    )
                    continue
                if item.task_id in {task.task_id for task in tasks}:
                    issues.append(
                        MigrationIssue(
                            legacy_task_id=item.task_id,
                            kind="taskboard_identity_collision",
                            detail="TaskBoard item collides with another migrated Task; board file preserved",
                        )
                    )
                    continue
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
        source_sha256=hashlib.sha256(runtime_bytes).hexdigest(),
        runtime_bytes=runtime_bytes,
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
    resolved_runtime = runtime_root.expanduser().resolve()
    resolved_workspace = workspace.expanduser().resolve()
    _assert_runtime_offline(resolved_runtime)
    if taskboard_path is None:
        selected_taskboard = discover_taskboard(resolved_runtime)
    else:
        selected_taskboard = taskboard_path.expanduser().resolve()
        if not selected_taskboard.exists():
            raise ValueError("Explicit TaskBoard source does not exist")
    return _build_plan(
        runtime_root=resolved_runtime,
        workspace=resolved_workspace,
        taskboard_path=selected_taskboard,
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
            prompt=run.prompt,
            request_id=run.request_id,
            source=run.source,
            mode=run.mode,
            result=run.result,
            budget=run.budget,
            trace_ref=run.trace_ref,
            session_id=run.session_id,
            checkpoint_receipt=run.checkpoint_receipt,
            created_at=run.created_at,
            metadata=run.metadata,
        )
    store.mark_migration(
        source_sha256=plan.source_sha256,
        taskboard_sha256=plan.taskboard_sha256,
        source=str(plan.runtime_root),
    )


def _verify_snapshot(plan: MigrationPlan) -> None:
    runtime_file = plan.runtime_root / "runtime.json"
    if runtime_file.read_bytes() != plan.runtime_bytes:
        raise RuntimeError("RuntimeCatalogue changed during migration")
    if (
        plan.taskboard_path is not None
        and plan.taskboard_bytes is not None
        and plan.taskboard_path.read_bytes() != plan.taskboard_bytes
    ):
        raise RuntimeError("TaskBoard changed during migration")


def apply_migration(
    plan: MigrationPlan,
    *,
    target: Path,
    backup_root: Path,
) -> MigrationReport:
    """Publish a verified domain store; source and existing targets never mutate."""
    target = target.expanduser().resolve()
    backup_root = backup_root.expanduser().resolve()
    _assert_runtime_offline(plan.runtime_root)
    _verify_snapshot(plan)
    if target.exists():
        existing = DomainStore(target)
        if existing.has_migration(plan.source_sha256, plan.taskboard_sha256):
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
        _verify_snapshot(plan)
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
