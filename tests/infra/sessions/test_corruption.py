"""Corrupt stored evidence fails closed; no older head is served."""

import hashlib
import json
from pathlib import Path

import pytest

from cairn.agent.session.codec import canonical
from cairn.agent.session.models import HostResolution, SessionStoreError
from cairn.agent.session.store import SessionStore
from tests.support.pg import database_for
from tests.support.sessions import (
    OTHER_TURN,
    SESSION,
    T4,
    binding,
    intent,
    settled,
    store,
)


@pytest.mark.parametrize(
    "damage",
    [
        "json",
        "digest",
        "extra",
        "duplicate",
        "format",
        "column_seq",
        "intent",
        "fence",
        "abandoned_commit",
    ],
)
def test_corruption_fails_closed_without_older_head_fallback(
    tmp_path: Path, damage: str
) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    revision = api.create(bind).revision
    turn = intent(bind, revision)
    revision = api.begin_turn(revision, turn)
    api.commit_turn(revision, settled(turn))
    if damage == "abandoned_commit":
        report = api.inspect(SESSION)
        extra = intent(
            report.binding, report.revision, turn_id=OTHER_TURN, start=4, time=T4
        )
        api.begin_turn(report.revision, extra)
        report = api.inspect(SESSION)
        revision = api.require_recovery(report.revision, "finalization_failed")
        api.release_turn(revision, extra.turn_id)  # no execution/cleanup is outstanding
        report = api.inspect(SESSION)
        api.reconcile(
            report.revision,
            HostResolution(
                session_id=SESSION,
                turn_id=extra.turn_id,
                actor="host",
                resolved_at_utc=T4,
                binding=report.binding,
            ),
        )
    api.close()
    with database_for(tmp_path).connect("agent") as conn:
        if damage in ("intent", "fence", "abandoned_commit"):
            selected_turn = OTHER_TURN if damage == "abandoned_commit" else turn.turn_id
            row = conn.execute(
                "SELECT diagnostics_json FROM turns WHERE turn_id=%s",
                (selected_turn,),
            ).fetchone()[0]
            data = json.loads(row)
            if damage == "intent":
                data["message_start"] = 9
            elif damage == "fence":
                data["fences"] = [{"unexpected": "field"}]
            else:
                data["commit_version"] = 123
            conn.execute(
                "UPDATE turns SET diagnostics_json=%s WHERE turn_id=%s",
                (canonical(data).decode(), selected_turn),
            )
        elif damage == "column_seq":
            conn.execute("UPDATE sessions SET head_seq=0")
        else:
            row = conn.execute(
                "SELECT payload FROM checkpoints WHERE seq=1"
            ).fetchone()[0]
            if damage == "json":
                row = b"{"
            elif damage == "digest":
                conn.execute(
                    "UPDATE checkpoints SET sha256=%s WHERE seq=1", ("0" * 64,)
                )
            elif damage == "extra":
                row = row[:-1] + b',"extra":true}'
            elif damage == "duplicate":
                row = row[:-1] + b',"seq":1}'
            elif damage == "format":
                row = row.replace(b'"format_version":2', b'"format_version":99')
            if damage != "digest":
                conn.execute(
                    "UPDATE checkpoints SET payload=%s,sha256=%s WHERE seq=1",
                    (row, hashlib.sha256(row).hexdigest()),
                )
    reader: SessionStore | None = None
    try:
        with pytest.raises(SessionStoreError):
            reader = store(tmp_path, mode="inspect")
            reader.inspect(SESSION)
    finally:
        if reader is not None:
            reader.close()
