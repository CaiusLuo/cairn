"""Legacy development_handoffs schema migration regression."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from cairn.conversation.store import DomainStore
from cairn.development.models import HandoffStatus


def _old_schema(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE conversations (
                conversation_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                source TEXT NOT NULL,
                initial_prompt TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );
            CREATE TABLE development_handoffs (
                run_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                request_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                runtime_task_id TEXT,
                domain_task_id TEXT,
                prompt TEXT NOT NULL,
                source TEXT NOT NULL,
                diagnostic TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );
            INSERT INTO conversations
                (conversation_id, workspace_id, source, initial_prompt,
                 created_at, updated_at, metadata_json)
            VALUES
                ('conv-old', 'ws-old', 'cli', 'old goal',
                 '2025-01-01T00:00:00+00:00', '2025-01-01T00:00:00+00:00', '{}');
            INSERT INTO development_handoffs
                (run_id, conversation_id, request_id, status, prompt, source,
                 created_at, updated_at, metadata_json)
            VALUES
                ('run-old', 'conv-old', 'handoff:run-old', 'needs_review',
                 'old goal', 'cli', '2025-01-01T00:00:00+00:00',
                 '2025-01-01T00:00:00+00:00', '{"legacy": true}');
            """
        )
        connection.commit()
    finally:
        connection.close()


def _columns(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {
            row[1]
            for row in connection.execute("PRAGMA table_info(development_handoffs)")
        }
    finally:
        connection.close()


def test_legacy_handoff_schema_migrates_idempotently_and_preserves_records(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-domain.sqlite3"
    _old_schema(path)

    first = DomainStore(path)
    record = first.get_handoff("run-old")
    assert record is not None
    assert record.status is HandoffStatus.NEEDS_REVIEW
    assert record.prompt == "old goal"
    assert record.history_origins == ()
    assert "history_origins_json" in _columns(path)

    second = DomainStore(path)
    again = second.get_handoff("run-old")
    assert again is not None
    assert again == record
    assert "history_origins_json" in _columns(path)
    # Existing legacy metadata remains readable after the additive migration.
    assert again.metadata == {"legacy": True}
