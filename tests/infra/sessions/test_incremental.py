"""V2 chain integrity, bounded reconstruction and preserved V1 evidence."""

import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest

from cairn.agent.models.messages import Message, ToolCall
from cairn.agent.session.codec import canonical, restore_messages
from cairn.agent.session.models import (
    MAX_CHECKPOINT_BYTES,
    SCHEMA_VERSION,
    CheckpointReceipt,
    HostResolution,
    SessionStoreError,
)
from cairn.agent.session.store import SessionStore
from cairn.shared.db import Database
from tests.support.pg import database_for
from tests.support.sessions import (
    SESSION,
    T4,
    binding,
    fence,
    intent,
    messages,
    settled,
    store,
)


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


def checkpoints(database: Database) -> list[tuple[int, bytes]]:
    with database.connect("agent") as connection:
        rows = connection.execute(
            "SELECT seq, payload FROM checkpoints ORDER BY seq"
        ).fetchall()
    return [(int(row["seq"]), bytes(row["payload"])) for row in rows]


def test_full_history_reopen_and_linear_v2_payloads(tmp_path: Path) -> None:
    api = store(tmp_path)
    api.create(binding(tmp_path))
    receipts = [append_turn(api, i) for i in range(8)]
    api.close()
    reader = store(tmp_path, mode="inspect")
    try:
        report = reader.inspect(SESSION)
        assert report.receipt == receipts[-1]
        assert restore_messages(report.checkpoint) == messages() * 8
        assert reader.schema_version == SCHEMA_VERSION
    finally:
        reader.close()
    data = [json.loads(raw) for _, raw in checkpoints(database_for(tmp_path))]
    assert [len(d["messages"]) for d in data] == [0] + [4] * 8
    for index in range(2, len(data)):
        assert data[index]["predecessor"] == receipts[index - 2].model_dump(mode="json")


@pytest.mark.parametrize(
    "damage",
    [
        "changed_tail",
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
    api.close()
    with database_for(tmp_path).connect("agent") as conn:
        raw = conn.execute("SELECT payload FROM checkpoints WHERE seq=2").fetchone()[0]
        data = json.loads(raw)
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
                conn.execute("SELECT payload FROM checkpoints WHERE seq=1").fetchone()[
                    0
                ]
            )["messages"]
            data["messages"] = prefix + data["messages"]
            conn.execute("UPDATE checkpoints SET format_version=1 WHERE seq=2")
        raw = canonical(data)
        conn.execute(
            "UPDATE checkpoints SET payload=%s,sha256=%s WHERE seq=2",
            (raw, hashlib.sha256(raw).hexdigest()),
        )
    reader: SessionStore | None = None
    try:
        with pytest.raises(SessionStoreError):
            reader = store(tmp_path, mode="inspect")
            reader.inspect(SESSION)
    finally:
        if reader is not None:
            reader.close()


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


def test_v2_rejects_total_materialized_history_even_when_each_record_fits(
    tmp_path: Path,
) -> None:
    # Bypass mutation admission to construct an over-limit, otherwise valid chain
    # with independent bounded records. Recovery must enforce the aggregate bound.
    api = store(tmp_path)
    api.create(binding(tmp_path))
    for index in range(10):
        append_turn(api, index)
    api.close()
    with database_for(tmp_path).connect("agent") as conn:
        predecessor = None
        for checkpoint_row in conn.execute(
            "SELECT seq,payload FROM checkpoints ORDER BY seq"
        ).fetchall():
            seq = checkpoint_row["seq"]
            data = json.loads(checkpoint_row["payload"])
            if seq:
                data["messages"][-1]["content"] = "x" * 900_000
                data["predecessor"] = predecessor
            encoded = canonical(data)
            assert len(encoded) < MAX_CHECKPOINT_BYTES
            digest = hashlib.sha256(encoded).hexdigest()
            conn.execute(
                "UPDATE checkpoints SET payload=%s,sha256=%s WHERE seq=%s",
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


def test_v2_turn_traversal_keeps_historical_attempt_order(tmp_path: Path) -> None:
    api = store(tmp_path)
    api.create(binding(tmp_path))
    for number in range(3):
        append_turn(api, number)
    assert [r.intent.base_seq for r in api.inspect(SESSION).attempts] == [0, 1, 2]
    api.close()
