"""V2 chain integrity, bounded reconstruction and preserved V1 evidence."""

import hashlib
import json
import sqlite3
from pathlib import Path
from uuid import UUID

import pytest

from cairn.core.models import Message, ToolCall
from cairn.infra.sessions.codec import canonical, restore_messages
from cairn.infra.sessions.models import (
    MAX_CHECKPOINT_BYTES,
    CheckpointReceipt,
    HostResolution,
    SessionStoreError,
)
from cairn.infra.sessions.store import SessionStore
from tests.support.sessions import (
    SESSION,
    T4,
    binding,
    fence,
    intent,
    legacy_store,
    messages,
    settled,
    store,
)
from tests.support.task_ledger import INPUT_DIGEST, TASK, commit_task_checkpoint


def append_turn(api: SessionStore, number: int) -> CheckpointReceipt:
    report = api.inspect(SESSION)
    turn = intent(
        report.binding,
        report.revision,
        turn_id=str(UUID(int=number + 100, version=4)),
        start=len(report.checkpoint.messages),
        time=T4,
    )
    active = api.begin_turn(report.revision, turn)
    active = api.fence_tool(
        active,
        fence(
            report.binding,
            turn_id=turn.turn_id,
            index=turn.message_start + 1,
        ).model_copy(update={"observed_at_utc": T4}),
    )
    return api.commit_turn(
        active,
        settled(turn, history=restore_messages(report.checkpoint) + messages()),
    )


def rows(path: Path, table: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as conn:
        return conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()


@pytest.mark.parametrize("version", [1, 2, 3])
def test_full_history_reopen_and_linear_v2_payloads(
    tmp_path: Path, version: int
) -> None:
    api = store(tmp_path) if version == 3 else legacy_store(tmp_path, version=version)
    api.create(binding(tmp_path))
    receipts = [append_turn(api, i) for i in range(8)]
    path = api.path
    api.close()
    original = {p.name: p.read_bytes() for p in path.parent.iterdir()}
    reader = store(tmp_path, mode="inspect")
    try:
        report = reader.inspect(SESSION)
        assert report.receipt == receipts[-1]
        assert restore_messages(report.checkpoint) == messages() * 8
        assert reader.schema_version == version
    finally:
        reader.close()
    assert {p.name: p.read_bytes() for p in path.parent.iterdir()} == original
    with sqlite3.connect(path) as conn:
        data = [
            json.loads(row[0])
            for row in conn.execute("SELECT payload FROM checkpoints ORDER BY seq")
        ]
    if version == 3:
        assert [len(d["messages"]) for d in data] == [0] + [4] * 8
        for index in range(2, len(data)):
            assert data[index]["predecessor"] == receipts[index - 2].model_dump(
                mode="json"
            )
    else:
        assert [len(d["messages"]) for d in data] == list(range(0, 33, 4))


def test_upgrade_preserves_historical_task_receipt_bytes_and_mixed_chain(
    tmp_path: Path,
) -> None:
    api = legacy_store(tmp_path)
    api.create(binding(tmp_path))
    task = api.create_task(
        api.inspect(SESSION).revision, task_id=TASK, input_digest=INPUT_DIGEST
    )
    old_receipt = commit_task_checkpoint(api, task, fenced=True)
    api.commit_task_turn(task.revision, old_receipt)
    append_turn(api, 2)
    before = {
        table: rows(api.path, table)
        for table in ("sessions", "turns", "checkpoints", "tasks")
    }
    report = api.inspect(SESSION)
    backup = api.upgrade_schema()
    assert backup.name.startswith("schema-2-backup-")
    assert api.schema_version == 3
    assert api.inspect(SESSION) == report
    for table, expected in before.items():
        assert rows(backup, table) == rows(api.path, table) == expected
    current = append_turn(api, 3)
    assert api.inspect_task(SESSION, TASK).task.checkpoint == old_receipt
    assert api.inspect(SESSION).receipt == current
    path = api.path
    api.close()
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT format_version FROM checkpoints ORDER BY seq"
        ).fetchall() == [(1,), (1,), (1,), (2,)]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    reader = store(tmp_path, mode="inspect")
    try:
        assert reader.inspect_task(SESSION, TASK).task.checkpoint == old_receipt
        assert restore_messages(reader.inspect(SESSION).checkpoint) == messages() * 3
    finally:
        reader.close()


@pytest.mark.parametrize(
    "damage",
    [
        "changed_tail",
        "missing_delta",
        "reordered_delta",
        "predecessor_digest",
        "predecessor_seq",
        "predecessor_turn",
        "turn_attribution",
        "range",
        "input",
        "tool_group",
        "fence",
        "downgrade",
    ],
)
def test_intermediate_corruption_with_recomputed_local_digest_fails_closed(
    tmp_path: Path, damage: str
) -> None:
    api = store(tmp_path)
    api.create(binding(tmp_path))
    for index in range(4):
        append_turn(api, index)
    path = api.path
    api.close()
    with sqlite3.connect(path) as conn:
        raw = conn.execute("SELECT payload FROM checkpoints WHERE seq=2").fetchone()[0]
        data = json.loads(raw)
        if damage == "missing_delta":
            conn.execute("DELETE FROM checkpoints WHERE seq=2")
        else:
            if damage == "changed_tail":
                data["messages"][-1]["content"] = "valid-looking changed history"
            elif damage == "reordered_delta":
                data["messages"] = data["messages"][::-1]
            elif damage == "predecessor_digest":
                data["predecessor"]["sha256"] = "e" * 64
            elif damage == "predecessor_seq":
                data["predecessor"]["revision"]["head_seq"] = 0
            elif damage == "predecessor_turn":
                data["predecessor"]["turn_id"] = str(UUID(int=999, version=4))
            elif damage == "turn_attribution":
                data["turn_id"] = str(UUID(int=999, version=4))
            elif damage == "range":
                data["message_range"] = {"start": 5, "end": 9}
            elif damage == "input":
                data["messages"][0]["content"] = "different input"
            elif damage == "tool_group":
                data["messages"][2]["tool_call_id"] = "wrong-call"
            elif damage == "fence":
                data["messages"][1]["tool_calls"][0]["name"] = "different_tool"
            else:
                data["format_version"] = 1
                del data["predecessor"]
                prefix = json.loads(
                    conn.execute(
                        "SELECT payload FROM checkpoints WHERE seq=1"
                    ).fetchone()[0]
                )["messages"]
                data["messages"] = prefix + data["messages"]
                conn.execute("UPDATE checkpoints SET format_version=1 WHERE seq=2")
            raw = canonical(data)
            conn.execute(
                "UPDATE checkpoints SET payload=?,sha256=? WHERE seq=2",
                (raw, hashlib.sha256(raw).hexdigest()),
            )
    before = {p.name: p.read_bytes() for p in path.parent.iterdir()}
    reader: SessionStore | None = None
    try:
        with pytest.raises(SessionStoreError):
            reader = store(tmp_path, mode="inspect")
            reader.inspect(SESSION)
    finally:
        if reader is not None:
            reader.close()
    assert {p.name: p.read_bytes() for p in path.parent.iterdir()} == before


def test_multiple_groups_empty_rollback_and_abandoned_turn_preserve_order(
    tmp_path: Path,
) -> None:
    api = store(tmp_path)
    api.create(binding(tmp_path))
    append_turn(api, 1)
    report = api.inspect(SESSION)
    history = restore_messages(report.checkpoint)
    turn = intent(
        report.binding,
        report.revision,
        turn_id=str(UUID(int=400, version=4)),
        start=4,
        time=T4,
    )
    active = api.begin_turn(report.revision, turn)
    # A rolled-back interrupted Turn contributes no messages but remains a link.
    receipt = api.commit_turn(
        active, settled(turn, history=history, outcome="runtime_error")
    )
    report = api.inspect(SESSION)
    turn = intent(
        report.binding,
        receipt.revision,
        turn_id=str(UUID(int=401, version=4)),
        start=4,
        time=T4,
    )
    active = api.begin_turn(report.revision, turn)
    for ordinal, index in enumerate((5, 7), 1):
        active = api.fence_tool(
            active,
            fence(
                report.binding, turn_id=turn.turn_id, index=index, ordinal=ordinal
            ).model_copy(update={"observed_at_utc": T4}),
        )
    extra = [
        Message(
            role="assistant",
            tool_calls=[
                ToolCall(id="call-1", name="read_file", arguments={"path": "b.py"})
            ],
        ),
        Message(role="tool", tool_call_id="call-1", content="second result"),
    ]
    history += messages()[:3] + extra  # interrupted after two closed groups
    api.commit_turn(active, settled(turn, history=history, outcome="cancelled"))
    report = api.inspect(SESSION)
    turn = intent(
        report.binding,
        report.revision,
        turn_id=str(UUID(int=402, version=4)),
        start=9,
        time=T4,
    )
    active = api.begin_turn(report.revision, turn)
    failed = api.require_recovery(active, "finalization_failed")
    api.release_turn(failed, turn.turn_id)
    report = api.inspect(SESSION)
    api.reconcile(
        report.revision,
        HostResolution(
            session_id=SESSION,
            turn_id=turn.turn_id,
            actor="host",
            resolved_at_utc=T4,
            binding=report.binding,
        ),
    )
    api.close()
    reader = store(tmp_path, mode="inspect")
    try:
        report = reader.inspect(SESSION)
        assert restore_messages(report.checkpoint) == history
        assert [attempt.state for attempt in report.attempts] == [
            "completed",
            "interrupted",
            "interrupted",
            "abandoned",
        ]
        assert report.revision.head_seq == 3
    finally:
        reader.close()


@pytest.mark.parametrize(
    "failure", ["ddl", "validation", "interrupt", "commit_before", "commit_after"]
)
def test_schema_two_migration_failure_retains_backup_and_foreign_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    api = legacy_store(tmp_path)
    api.create(binding(tmp_path))
    receipt = append_turn(api, 1)
    original = rows(api.path, "checkpoints")
    migrate = api._migrate_v2

    def fail(conn: sqlite3.Connection) -> None:
        migrate(conn)
        if failure == "validation":
            conn.execute("UPDATE checkpoints SET sha256=? WHERE seq=1", ("0" * 64,))
        elif failure == "interrupt":
            raise KeyboardInterrupt
        else:
            raise sqlite3.OperationalError("private failure")

    def uncertain(conn: sqlite3.Connection) -> None:
        if failure == "commit_after":
            conn.commit()
        raise sqlite3.OperationalError("private failure")

    if failure.startswith("commit"):
        monkeypatch.setattr(api, "_commit", uncertain)
    else:
        monkeypatch.setattr(api, "_migrate_v2", fail)
    with pytest.raises(
        KeyboardInterrupt if failure == "interrupt" else SessionStoreError
    ):
        api.upgrade_schema()
    assert api.schema_version == (3 if failure == "commit_after" else 2)
    assert api._conn().execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert api._conn().execute("PRAGMA foreign_key_check").fetchall() == []
    assert rows(api.path, "checkpoints") == original
    assert api.inspect(SESSION).receipt == receipt
    saved = list(api.path.parent.glob("schema-2-backup-*.sqlite3"))
    assert len(saved) == 1 and rows(saved[0], "checkpoints") == original
    api.close()


def test_v2_rejects_total_materialized_history_even_when_each_record_fits(
    tmp_path: Path,
) -> None:
    # Bypass mutation admission to construct an over-limit, otherwise valid chain
    # with independent bounded records. Recovery must enforce the aggregate bound.
    api = store(tmp_path)
    api.create(binding(tmp_path))
    for index in range(10):
        append_turn(api, index)
    path = api.path
    api.close()
    with sqlite3.connect(path) as conn:
        predecessor = None
        for seq, raw in conn.execute(
            "SELECT seq,payload FROM checkpoints ORDER BY seq"
        ).fetchall():
            data = json.loads(raw)
            if seq:
                data["messages"][-1]["content"] = "x" * 900_000
                data["predecessor"] = predecessor
            encoded = canonical(data)
            assert len(encoded) < MAX_CHECKPOINT_BYTES
            digest = hashlib.sha256(encoded).hexdigest()
            conn.execute(
                "UPDATE checkpoints SET payload=?,sha256=? WHERE seq=?",
                (encoded, digest, seq),
            )
            predecessor = {
                "revision": {
                    "session_id": SESSION,
                    "version": seq * 3,
                    "head_seq": seq,
                },
                "turn_id": data["turn_id"],
                "sha256": digest,
                "outcome": data["outcome"],
            }
    reader = store(tmp_path, mode="inspect")
    try:
        with pytest.raises(SessionStoreError, match="record_limit"):
            reader.inspect(SESSION)
    finally:
        reader.close()


def test_failed_foreign_key_restore_blocks_even_recovery_mutations(
    tmp_path: Path,
) -> None:
    api = legacy_store(tmp_path)
    api.create(binding(tmp_path))
    append_turn(api, 1)
    conn = api._conn()

    def deny_restore(
        action: int,
        name: str | None,
        value: str | None,
        database: str | None,
        source: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_PRAGMA and name == "foreign_keys" and value == "ON":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(deny_restore)
    with pytest.raises(SessionStoreError):
        api.upgrade_schema()
    conn.set_authorizer(None)
    report = api.inspect(SESSION)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    with pytest.raises(SessionStoreError, match="sqlite_configuration_unavailable"):
        api.require_recovery(report.revision, "storage_failed")
    assert api.inspect(SESSION).revision == report.revision
    api.close()


def test_v2_turn_traversal_does_not_sort_historical_attempts(tmp_path: Path) -> None:
    api = store(tmp_path)
    api.create(binding(tmp_path))
    for number in range(3):
        append_turn(api, number)
    plan = (
        api._conn()
        .execute(
            "EXPLAIN QUERY PLAN SELECT * FROM turns WHERE session_id=? ORDER BY rowid",
            (SESSION,),
        )
        .fetchall()
    )
    assert all("TEMP B-TREE" not in row[3] for row in plan)
    assert [r.intent.base_seq for r in api.inspect(SESSION).attempts] == [0, 1, 2]
    api.close()
