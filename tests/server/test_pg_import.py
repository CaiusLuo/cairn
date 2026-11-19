from pathlib import Path

import pytest

from cairn.agent.session.access import open_store
from cairn.channels.store import ChannelStore
from cairn.conversation.store import DomainStore
from cairn.development.catalogue import RuntimeCatalogue
from cairn.knowledge.store import KnowledgeStore
from cairn.review.store import ReviewStore
from cairn.scheduling.store import ScheduleStore
from cairn.server.pg_import import import_state
from tests.support.legacy_state import SECRET, write_legacy_state
from tests.support.pg import database_for


def test_import_moves_every_component_and_verifies(tmp_path: Path) -> None:
    state = write_legacy_state(tmp_path / "state")
    database = database_for(tmp_path / "target")

    report = import_state(state, database)

    assert report.dry_run is False
    assert report.skipped_files == ()
    assert report.credentials == 1
    assert report.tables
    assert all(table.verified for table in report.tables)

    domain = DomainStore(database)
    conversation = domain.get_conversation("conv-1")
    assert conversation is not None and conversation.initial_prompt == "hello"
    run = domain.get_run("run-1")
    assert run is not None and run.status.value == "completed"
    assert run.metadata == {"x": 1}
    assert len(domain.list_run_events("run-1")) == 2
    assert domain.get_task("task-1") is not None

    sessions = open_store(database, inspect=True)
    assert [item.session_id for item in sessions.list_sessions()] == ["session-1"]
    assert sessions.list_sessions()[0].head_seq == 1

    knowledge = KnowledgeStore(database)
    clip = knowledge.get("clip-1")
    assert clip is not None and clip.content == "content"
    review = ReviewStore(knowledge).get("review-1")
    assert review is not None and review.status == "completed"

    schedule = ScheduleStore(database)
    assert [job.job_id for job in schedule.load_jobs()] == ["job-1"]
    assert schedule.load_runs()[0]["job_id"] == "job-1"
    assert schedule.load_state("review-schedule") == {
        "version": 1,
        "initialized": True,
    }
    assert schedule.load_state("weekly-review-schedule") == {"version": 1}
    assert RuntimeCatalogue(database).data.tasks == {}

    channels = ChannelStore(database, state / "channels")
    connection = channels.get_connection("conn-1")
    assert connection is not None
    assert connection.credential_ref is not None
    assert connection.credential_ref.startswith("cred-")
    secret = channels.vault.load(connection.credential_ref)
    assert secret is not None and secret["bot_token"] == SECRET
    with database.connect("channel") as db:
        names = {
            str(row[0])
            for row in db.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = %s",
                (database.schema("channel"),),
            )
        }
        stored = db.execute(
            "SELECT credential_ref FROM channel_connections WHERE connection_id = %s",
            ("conn-1",),
        ).fetchone()
    assert "channel_credentials" not in names
    assert SECRET not in str(stored)


def test_import_refuses_an_occupied_target_unless_replaced(tmp_path: Path) -> None:
    state = write_legacy_state(tmp_path / "state")
    database = database_for(tmp_path / "target")
    first = import_state(state, database)

    with pytest.raises(ValueError, match="Target already contains rows"):
        import_state(state, database)

    second = import_state(state, database, replace=True)
    assert second.replaced is True
    assert [(table.table, table.rows) for table in second.tables] == [
        (table.table, table.rows) for table in first.tables
    ]
    assert DomainStore(database).get_conversation("conv-1") is not None


def test_two_rehearsals_agree(tmp_path: Path) -> None:
    state = write_legacy_state(tmp_path / "state")

    first = import_state(state, None, dry_run=True)
    second = import_state(state, None, dry_run=True)

    assert first.model_dump() == second.model_dump()


def test_dry_run_reports_without_touching_state_or_database(tmp_path: Path) -> None:
    state = write_legacy_state(tmp_path / "state")
    report = import_state(state, None, dry_run=True)

    assert report.dry_run is True
    assert report.credentials == 1
    assert all(not table.verified for table in report.tables)
    assert not (state / "channels" / "credentials").exists()


def test_import_rejects_a_legacy_schema_that_was_never_upgraded(
    tmp_path: Path,
) -> None:
    import sqlite3

    state = write_legacy_state(tmp_path / "state")
    connection = sqlite3.connect(state / "runtime" / "sessions" / "sessions.sqlite3")
    try:
        connection.execute("PRAGMA user_version=3")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ValueError, match="schema version 3; expected 4"):
        import_state(state, None, dry_run=True)


def test_missing_legacy_files_are_reported_not_invented(tmp_path: Path) -> None:
    state = tmp_path / "empty"
    state.mkdir()
    report = import_state(state, None, dry_run=True)

    assert report.tables == ()
    assert str(state / "runtime" / "domain.sqlite3") in report.skipped_files
    assert str(state / "knowledge" / "knowledge.sqlite3") in report.skipped_files


def test_separate_legacy_chat_store_must_be_resolved_first(tmp_path: Path) -> None:
    state = write_legacy_state(tmp_path / "state")
    separate = state / "runtime" / "sessions-chat"
    separate.mkdir()
    (separate / "sessions.sqlite3").write_bytes(b"prior release chat history")

    with pytest.raises(ValueError, match="Separate legacy chat store"):
        import_state(state, None, dry_run=True)
