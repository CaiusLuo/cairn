from pathlib import Path

import pytest

from cairn.agent.models.messages import Message
from cairn.development.board.models import TaskBoardStatus, TaskSource
from cairn.development.board.store import TaskBoardStore
from cairn.interfaces.web.config import WebConfig
from cairn.interfaces.web.reader import SnapshotReader
from tests.support.pg import database_for
from tests.support.sessions import SESSION, binding, intent, settled, store


def _seed_session(root: Path) -> None:
    api = store(root)
    try:
        bind = binding(root)
        api.create(bind)
        initial = api.inspect(SESSION).revision
        turn = intent(bind, initial, user="hello")
        active = api.begin_turn(initial, turn)
        history = [
            Message(role="user", content="hello"),
            Message(role="assistant", content="done"),
        ]
        api.commit_turn(active, settled(turn, history=history))
    finally:
        api.close()


def _reader(root: Path) -> SnapshotReader:
    return SnapshotReader(
        workspace=root / "work",
        database=database_for(root),
        tasks_dir=root / "tasks",
    )


def test_snapshot_reads_real_session_and_task_facts(tmp_path: Path) -> None:
    _seed_session(tmp_path)
    board = TaskBoardStore(tmp_path / "tasks")
    task = board.create(
        title="Web surface prototype",
        source=TaskSource.CLI,
        session_id=SESSION,
    )
    board.add_todo(task.task_id, "check reader")
    board.update(task.task_id, status=TaskBoardStatus.RUNNING)

    first = _reader(tmp_path).snapshot()
    assert first["version"] == 1
    assert first["read_only"] is True
    assert first["source_status"] == {"sessions": "ok", "tasks": "ok"}
    assert first["sessions"][0]["session_id"] == SESSION
    assert first["sessions"][0]["source"] == "cli"
    assert first["sessions"][0]["task_ids"] == [task.task_id]
    assert first["tasks"][0]["status"] == "running"
    assert first["tasks"][0]["todos"][0]["text"] == "check reader"

    reader = _reader(tmp_path)
    second = reader.snapshot()
    assert second["sequence"] == first["sequence"]
    board.update(task.task_id, status=TaskBoardStatus.COMPLETED)
    third = reader.snapshot()
    assert third["sequence"] > second["sequence"]
    assert third["tasks"][0]["status"] == "completed"


def test_session_detail_returns_committed_messages(tmp_path: Path) -> None:
    _seed_session(tmp_path)
    detail = _reader(tmp_path).session_detail(SESSION)
    assert detail is not None
    assert detail["messages"] == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "done"},
    ]
    assert detail["recovery_status"] == "clean"


def test_missing_roots_are_explicit_not_exceptions(tmp_path: Path) -> None:
    snapshot = SnapshotReader(
        workspace=tmp_path,
        database=database_for(tmp_path),
        tasks_dir=tmp_path / "missing-tasks",
    ).snapshot()
    assert snapshot["source_status"] == {"sessions": "ok", "tasks": "missing"}
    assert snapshot["sessions"] == []
    assert snapshot["tasks"] == []


def test_malformed_task_board_reports_unavailable(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "tasks.json").write_text("{not-json", encoding="utf-8")
    snapshot = SnapshotReader(
        workspace=tmp_path,
        database=database_for(tmp_path),
        tasks_dir=tasks,
    ).snapshot()
    assert snapshot["source_status"]["tasks"] == "unavailable"


def test_web_config_rejects_unsafe_roots_and_non_loopback_host(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(ValueError):
        WebConfig(
            workspace=workspace,
            tasks_dir=workspace / "tasks",
        )
    with pytest.raises(ValueError):
        WebConfig(
            workspace=workspace,
            tasks_dir=tmp_path / "tasks",
            knowledge_dir=workspace / "knowledge",
        )
    with pytest.raises(ValueError):
        WebConfig(
            workspace=workspace,
            tasks_dir=tmp_path / "tasks",
            knowledge_dir=tmp_path / "tasks",
        )
    with pytest.raises(ValueError):
        WebConfig(
            workspace=workspace,
            tasks_dir=tmp_path / "tasks",
            host="0.0.0.0",
        )


def test_sequence_advances_when_source_status_changes_with_empty_lists(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    reader = SnapshotReader(
        workspace=workspace,
        database=database_for(tmp_path),
        tasks_dir=tmp_path / "tasks",
    )
    first = reader.snapshot()
    assert first["source_status"] == {"sessions": "ok", "tasks": "missing"}

    # An empty but readable board is an "ok" source with no rows; the sequence
    # must still change because the source status is part of the state.
    (tmp_path / "tasks").mkdir()
    second = reader.snapshot()
    assert second["source_status"] == {"sessions": "ok", "tasks": "ok"}
    assert second["sessions"] == []
    assert second["sequence"] > first["sequence"]
