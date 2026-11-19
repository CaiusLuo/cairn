"""Deterministic host facts and PostgreSQL crash fixtures (no providers)."""

import hashlib
import multiprocessing
import os
import time
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Literal

from cairn.agent.models.messages import Message, ToolCall
from cairn.agent.session.codec import snapshot_messages
from cairn.agent.session.models import (
    CheckpointPayload,
    MessageRange,
    Outcome,
    PathIdentity,
    Revision,
    SessionBinding,
    SettledTurn,
    ToolFence,
    TurnIntent,
    WorkspaceBinding,
)
from cairn.agent.session.store import SessionStore
from cairn.shared.db import Database
from tests.support.pg import database_for

SESSION = "00000000-0000-4000-8000-000000000001"
TURN = "00000000-0000-4000-8000-000000000002"
OTHER_TURN = "00000000-0000-4000-8000-000000000003"
T0 = "2020-01-01T00:00:00Z"
T1 = "2020-01-01T00:00:01Z"
T2 = "2020-01-01T00:00:02Z"
T3 = "2020-01-01T00:00:03Z"
T4 = "2020-01-01T00:00:04Z"


def binding(root: Path, *, session_id: str = SESSION) -> SessionBinding:
    work = root.resolve() / "work"
    work.mkdir(exist_ok=True)
    info = work.stat()
    return SessionBinding(
        session_id=session_id,
        workspace=WorkspaceBinding(
            root=PathIdentity(path=str(work), device=info.st_dev, inode=info.st_ino),
            git=None,
        ),
        provider=None,
        observed_at_utc=T0,
    )


def store(
    root: Path,
    *,
    database: Database | None = None,
    mode: Literal["writer", "inspect"] = "writer",
    sensitive_values: tuple[str, ...] = (),
) -> SessionStore:
    """Build a store against the PostgreSQL namespace for this host path."""
    return SessionStore(
        database or database_for(root),
        mode=mode,
        sensitive_values=sensitive_values,
    )


def intent(
    bind: SessionBinding,
    expected: Revision,
    *,
    turn_id: str = TURN,
    start: int = 0,
    user: str = "hello",
    time: str = T1,
) -> TurnIntent:
    return TurnIntent(
        session_id=bind.session_id,
        turn_id=turn_id,
        base_seq=expected.head_seq,
        message_start=start,
        input_digest=hashlib.sha256(user.encode()).hexdigest(),
        started_at_utc=time,
        binding=bind,
    )


def messages(user: str = "hello") -> list[Message]:
    return [
        Message(role="user", content=user),
        Message(
            role="assistant",
            content=None,
            tool_calls=[
                ToolCall(id="call-1", name="read_file", arguments={"path": "a.py"})
            ],
        ),
        Message(role="tool", tool_call_id="call-1", content="observed"),
        Message(role="assistant", content="done"),
    ]


def fence(
    bind: SessionBinding,
    *,
    ordinal: int = 1,
    turn_id: str = TURN,
    index: int = 1,
    call_id: str = "call-1",
    tool_name: str = "read_file",
) -> ToolFence:
    return ToolFence(
        session_id=bind.session_id,
        turn_id=turn_id,
        ordinal=ordinal,
        assistant_message_index=index,
        call_id=call_id,
        tool_name=tool_name,
        observed_at_utc=T2,
    )


def settled(
    turn: TurnIntent,
    *,
    history: list[Message] | None = None,
    outcome: Outcome = "completed",
    time: str = T4,
) -> SettledTurn:
    values = messages() if history is None else history
    bind = turn.binding.model_copy(update={"observed_at_utc": time})
    return SettledTurn(
        checkpoint=CheckpointPayload(
            session_id=turn.session_id,
            turn_id=turn.turn_id,
            seq=turn.base_seq + 1,
            binding=bind,
            messages=snapshot_messages(values),
            message_range=MessageRange(start=turn.message_start, end=len(values)),
            outcome=outcome,
            started_at_utc=turn.started_at_utc,
            settled_at_utc=time,
        )
    )


def crash_writer(root_text: str, window: str, barrier: Connection) -> None:
    """Parent kills after an explicit API barrier or while PostgreSQL blocks it."""
    root = Path(root_text)
    bind = binding(root)
    api = store(root)

    def stop(name: str) -> None:
        barrier.send(name)
        barrier.recv()  # parent SIGKILLs this process at the named boundary

    if window == "before_intent":
        stop(window)
    initial = api.inspect(SESSION).revision
    turn = intent(bind, initial)
    active = api.begin_turn(initial, turn)
    if window == "intent":
        stop(window)
    active = api.fence_tool(active, fence(bind))
    if window == "fence":
        stop(window)
    # A local external-effect double: exactly one append after the fence.
    with (root / "work" / "effect.txt").open("a") as output:
        output.write("one effect\n")
        output.flush()
        os.fsync(output.fileno())
    # For checkpoint_insert/attempt_update/before_commit the parent has installed
    # a blocking trigger, so this call never returns: the parent kills us while
    # the transaction is mid-write and PostgreSQL rolls it back.
    api.commit_turn(active, settled(turn))
    stop("committed")


def _barrier_key(root: Path) -> int:
    digest = hashlib.sha256(f"{root.resolve()}:crash".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _barrier_table(window: str) -> str | None:
    return {
        "checkpoint_insert": "checkpoints",
        "attempt_update": "sessions",
        "before_commit": "sessions",
    }.get(window)


def _install_barrier(
    database: Database,
    key: int,
    table: str,
    *,
    events: str = "INSERT OR UPDATE",
    when: str = "",
) -> None:
    schema = database.schema("agent")
    connection = database.dedicated("agent")
    try:
        connection.execute(
            f"CREATE OR REPLACE FUNCTION {schema}.crash_barrier() "
            "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            f"PERFORM pg_advisory_lock({key}); RETURN NEW; END $$"
        )
        connection.execute(f"DROP TRIGGER IF EXISTS crash_barrier ON {schema}.{table}")
        connection.execute(
            f"CREATE TRIGGER crash_barrier BEFORE {events} ON "
            f"{schema}.{table} FOR EACH ROW {when} "
            f"EXECUTE FUNCTION {schema}.crash_barrier()"
        )
    finally:
        connection.close()


def _drop_barrier(database: Database, table: str) -> None:
    schema = database.schema("agent")
    connection = database.dedicated("agent")
    try:
        connection.execute(f"DROP TRIGGER IF EXISTS crash_barrier ON {schema}.{table}")
        connection.execute(f"DROP FUNCTION IF EXISTS {schema}.crash_barrier()")
    finally:
        connection.close()


def _waiter_present(database: Database, key: int) -> bool:
    high = (key >> 32) & 0xFFFFFFFF
    low = key & 0xFFFFFFFF
    with database.connect("agent") as connection:
        row = connection.execute(
            "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
            "AND classid = %s AND objid = %s AND objsubid = 1 "
            "AND NOT granted LIMIT 1",
            (high, low),
        ).fetchone()
    return row is not None


def crash_at(root: Path, window: str) -> None:
    """Terminate an owned writer at the existing crash barrier, not by close()."""
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    database = database_for(root)
    key = _barrier_key(root)
    table = _barrier_table(window)
    blocker = None
    if table is not None:
        # Only the terminal CAS advances head_seq; session writes before it
        # (begin, fence) must pass so the crash lands at commit time.
        if table == "sessions":
            _install_barrier(
                database,
                key,
                table,
                events="UPDATE",
                when="WHEN (NEW.head_seq IS DISTINCT FROM OLD.head_seq)",
            )
        else:
            _install_barrier(database, key, table)
        blocker = database.dedicated("agent")
        blocker.execute("SELECT pg_advisory_lock(%s)", (key,))
    worker = context.Process(target=crash_writer, args=(str(root), window, child))
    try:
        worker.start()
        child.close()
        if table is not None:
            deadline = time.monotonic() + 15
            while not _waiter_present(database, key):
                assert time.monotonic() < deadline, f"missing blocked {window}"
                time.sleep(0.02)
        else:
            assert parent.poll(15), f"missing barrier; child exit={worker.exitcode}"
            assert parent.recv() == window
    finally:
        worker.kill()
        worker.join(10)
        if blocker is not None:
            blocker.execute("SELECT pg_advisory_unlock(%s)", (key,))
            blocker.close()
        if table is not None:
            _drop_barrier(database, table)
        parent.close()
    assert worker.exitcode == -9
