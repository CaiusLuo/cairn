"""Conversation/Task/Run identity and authority boundaries."""

from pathlib import Path

import pytest

from cairn.core.domain import (
    ConversationSource,
    RunStatus,
    TaskStatus,
    workspace_id_for,
)
from cairn.core.task_board import TaskBoardItem, TaskBoardStatus, TaskSource
from cairn.infra.domain_store import DomainStore
from cairn.infra.task_board.store import TaskBoardStore


def store(tmp_path: Path) -> tuple[DomainStore, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return DomainStore(tmp_path / "domain.sqlite3"), workspace


def test_ids_are_distinct_and_relationships_are_many_to_many(tmp_path: Path) -> None:
    domain, workspace = store(tmp_path)
    conversation = domain.create_conversation(
        workspace=workspace,
        source=ConversationSource.CLI,
        initial_prompt="Build it",
    )
    other = domain.create_conversation(
        workspace=workspace,
        source=ConversationSource.WEB,
        initial_prompt="Another",
    )
    first = domain.create_task(
        conversation_id=conversation.conversation_id,
        title="First",
        goal="First goal",
    )
    second = domain.create_task(
        conversation_id=conversation.conversation_id,
        title="Second",
        goal="Second goal",
    )
    first_run = domain.create_run(
        conversation_id=conversation.conversation_id,
        task_id=first.task_id,
    )
    second_run = domain.create_run(
        conversation_id=conversation.conversation_id,
        task_id=first.task_id,
    )
    other_run = domain.create_run(
        conversation_id=other.conversation_id,
    )
    assert len({conversation.conversation_id, first.task_id, first_run.run_id}) == 3
    assert (
        len({first.task_id, second.task_id, first_run.run_id, second_run.run_id}) == 4
    )
    assert len(domain.list_tasks_for_conversation(conversation.conversation_id)) == 2
    assert len(domain.list_runs_for_conversation(conversation.conversation_id)) == 2
    assert len(domain.list_runs_for_task(first.task_id)) == 2
    assert other_run.task_id is None
    assert domain.get_run(other_run.run_id) == other_run


def test_run_completion_never_changes_task_done_or_closed(tmp_path: Path) -> None:
    domain, workspace = store(tmp_path)
    conversation = domain.create_conversation(
        workspace=workspace,
        source=ConversationSource.CLI,
        initial_prompt="Implement",
    )
    task = domain.create_task(
        conversation_id=conversation.conversation_id,
        title="Implement",
        goal="Implement it",
    )
    run = domain.create_run(
        conversation_id=conversation.conversation_id,
        task_id=task.task_id,
    )
    domain.update_run(run.run_id, status=RunStatus.COMPLETED)
    current = domain.get_task(task.task_id)
    assert current is not None and current.status is TaskStatus.TODO
    assert domain.mark_task_done(task.task_id).status is TaskStatus.DONE
    assert domain.close_task(task.task_id).status is TaskStatus.CLOSED


def test_taskboard_projection_preserves_user_metadata(tmp_path: Path) -> None:
    board = TaskBoardStore(tmp_path / "board")
    original = board.create(
        title="User title",
        description="User description",
        project="project",
        source=TaskSource.WEB,
        issue_url="https://example.invalid/issue/1",
    )
    with_todo = board.add_todo(original.task_id, "user todo")
    assert with_todo is not None
    original_task = with_todo
    projection = TaskBoardItem(
        task_id=original.task_id,
        project="project",
        title="Runtime title",
        description="Runtime description",
        source=TaskSource.CLI,
        session_id="session-1",
        status=TaskBoardStatus.RUNNING,
        progress=("执行状态: running",),
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    projected = board.project_execution(projection)
    assert projected.title == "User title"
    assert projected.description == "User description"
    assert projected.todos == original_task.todos
    assert projected.issue_url == original_task.issue_url
    assert projected.source is TaskSource.WEB
    assert projected.status is TaskBoardStatus.RUNNING
    assert projected.session_id == "session-1"
    assert "执行状态: running" in projected.progress
    assert projected.revision == original_task.revision + 1


def test_workspace_identity_is_stable(tmp_path: Path) -> None:
    first = workspace_id_for(tmp_path)
    second = workspace_id_for(tmp_path)
    assert first == second
    assert first.startswith("ws-")


@pytest.mark.parametrize(
    "status", [RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED]
)
def test_run_terminal_states_are_independent_of_task_status(
    tmp_path: Path, status: RunStatus
) -> None:
    domain, workspace = store(tmp_path)
    conversation = domain.create_conversation(
        workspace=workspace,
        source=ConversationSource.CLI,
        initial_prompt="x",
    )
    task = domain.create_task(conversation_id=conversation.conversation_id, goal="goal")
    run = domain.create_run(
        conversation_id=conversation.conversation_id, task_id=task.task_id
    )
    domain.update_run(run.run_id, status=status)
    current = domain.get_task(task.task_id)
    assert current is not None and current.status is TaskStatus.TODO


def test_domain_store_rejects_symlink_before_resolving(tmp_path: Path) -> None:
    target = tmp_path / "real.sqlite3"
    target.write_text("not actually sqlite")
    link = tmp_path / "domain-link.sqlite3"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        DomainStore(link)
    assert link.is_symlink()
