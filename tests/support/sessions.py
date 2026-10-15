"""Deterministic host facts and SQLite fault/process fixtures (no providers)."""

import hashlib
import multiprocessing
import os
import sqlite3
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Literal

from cairn.core.models import Message, ToolCall
from cairn.infra.sessions.codec import snapshot_messages
from cairn.infra.sessions.models import (
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
from cairn.infra.sessions.store import SessionStore

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


#: The suite's one declaration that the development runtime may be unverified.
#: Production keeps the fail-closed default; this exists so the real
#: WAL/CAS/lock/crash regressions still run on a development interpreter.
UNVERIFIED_RUNTIME = True


def store(
    root: Path,
    *,
    mode: Literal["writer", "inspect"] = "writer",
    sensitive_values: tuple[str, ...] = (),
    allow_unverified: bool = UNVERIFIED_RUNTIME,
) -> SessionStore:
    """Build a store for the suite, declaring the local runtime if needed."""
    return SessionStore(
        root.resolve() / "store",
        mode=mode,
        forbidden_roots=(
            root.resolve() / "work",
            root.resolve() / "tmp",
            root.resolve() / "cache",
        ),
        sensitive_values=sensitive_values,
        allow_unverified=allow_unverified,
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
    """Parent kills only after an explicit SQL/API barrier, never a timed sleep."""
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
    assert api._connection is not None
    if window in ("checkpoint_insert", "attempt_update", "before_commit"):
        blocked = "turns" if window == "checkpoint_insert" else "sessions"

        def authorize(
            action: int,
            table: str | None,
            column: str | None,
            database: str | None,
            source: str | None,
        ) -> int:
            if (
                window != "before_commit"
                and action == sqlite3.SQLITE_UPDATE
                and table == blocked
            ) or (
                window == "before_commit"
                and action == sqlite3.SQLITE_TRANSACTION
                and table == "COMMIT"
            ):
                stop(window)
            return sqlite3.SQLITE_OK

        api._connection.set_authorizer(authorize)
    api.commit_turn(active, settled(turn))
    stop("committed")


def crash_at(root: Path, window: str) -> None:
    """Terminate an owned writer at the existing crash barrier, not by close()."""
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    worker = context.Process(target=crash_writer, args=(str(root), window, child))
    worker.start()
    child.close()
    try:
        assert parent.poll(15), f"missing barrier; child exit={worker.exitcode}"
        assert parent.recv() == window
    finally:
        worker.kill()
        worker.join(10)
        parent.close()
    assert worker.exitcode == -9


def competing_writer(root_text: str, barrier: Connection) -> None:
    from cairn.infra.sessions.models import SessionStoreError

    try:
        api = store(Path(root_text))
    except SessionStoreError as error:
        barrier.send(error.code)
    else:
        try:
            api.create(binding(Path(root_text), session_id=OTHER_TURN))
            barrier.send("writer_acquired")
        finally:
            api.close()


def assert_competing_writer(
    root: Path, expected: Literal["writer_busy", "writer_acquired"]
) -> None:
    """Probe the actual lifetime flock in a fresh process, never a timed race."""
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    worker = context.Process(target=competing_writer, args=(str(root), child))
    worker.start()
    child.close()
    try:
        assert parent.poll(15), f"missing writer probe; child exit={worker.exitcode}"
        assert parent.recv() == expected
    finally:
        worker.join(10)
        if worker.is_alive():
            worker.kill()
            worker.join(10)
        parent.close()
    assert worker.exitcode == 0


def inspection_writer(
    root_text: str, barrier: Connection, settle_before_probe: bool = False
) -> None:
    """A live writer paused before COMMIT or commanded while a reader owns SH."""
    from cairn.infra.sessions.models import SessionStoreError

    root = Path(root_text)
    bind = binding(root)
    api = store(root)
    try:
        initial = api.inspect(SESSION).revision
        turn = intent(bind, initial)
        active = api.fence_tool(api.begin_turn(initial, turn), fence(bind))
        if settle_before_probe:
            active = api.commit_turn(active, settled(turn)).revision
        barrier.send(("ready" if settle_before_probe else "active", active.version))
        command = barrier.recv()
        if command == "commit":
            assert api._connection is not None

            def authorize(
                action: int,
                table: str | None,
                column: str | None,
                database: str | None,
                source: str | None,
            ) -> int:
                if action == sqlite3.SQLITE_TRANSACTION and table == "COMMIT":
                    barrier.send("before_commit")
                    assert barrier.recv() == "release"
                return sqlite3.SQLITE_OK

            api._connection.set_authorizer(authorize)
            receipt = api.commit_turn(active, settled(turn))
            api._connection.set_authorizer(None)
            barrier.send(("committed", receipt.revision.version))
        else:
            assert command in ("fence", "close_attempt")
            try:
                if command == "fence":
                    api.fence_tool(active, fence(bind, ordinal=2, call_id="call-2"))
                else:
                    api.close()
            except SessionStoreError as error:
                barrier.send(error.code)
            else:
                barrier.send("unexpected_mutation")
        assert barrier.recv() == "close"
        if command == "fence":
            # The probe has finished all its work; guard contention faulted this
            # Turn. Explicitly release it before gracefully closing the writer.
            api.release_turn(active, turn.turn_id)
    finally:
        api.close()
    barrier.send("closed")


def legacy_store(root: Path, *, version: int = 2) -> SessionStore:
    """Seed an EMPTY historical schema, before any checkpoint is written.

    Never relabel or rewrite V2 facts as V1 fixtures. Production has no downgrade.
    """
    from cairn.infra.sessions.store import _SCHEMA

    api = store(root)
    api.close()
    with sqlite3.connect(api.path) as conn:
        assert conn.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 0
        conn.execute("DROP TABLE checkpoints")
        conn.execute("DROP INDEX session_turn_order")
        conn.execute(_SCHEMA[-1])
        if version == 1:
            conn.execute("DROP TABLE tasks")
        conn.execute(f"PRAGMA user_version={version}")
    return store(root)
