"""Offline Task/checkpoint facts and native process-death barriers."""

import hashlib
import multiprocessing
import os
import time
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Literal

from cairn.agent.session.codec import restore_messages
from cairn.agent.session.models import CheckpointReceipt, Outcome
from cairn.agent.session.store import SessionStore
from cairn.agent.session.task_models import TaskRecord
from tests.support.pg import database_for
from tests.support.sessions import (
    T4,
    TURN,
    _barrier_key,
    _drop_barrier,
    _install_barrier,
    _waiter_present,
    fence,
    intent,
    messages,
    settled,
    store,
)

TASK = "task-1"
INPUT_DIGEST = hashlib.sha256(b"hello").hexdigest()


def commit_task_checkpoint(
    api: SessionStore,
    task: TaskRecord,
    *,
    turn_id: str = TURN,
    task_id: str | None = TASK,
    user: str = "hello",
    fenced: bool = False,
    outcome: Outcome = "completed",
    effect_path: Path | None = None,
) -> CheckpointReceipt:
    report = api.inspect(task.revision.session_id)
    start = len(report.checkpoint.messages)
    turn = intent(
        report.binding,
        report.revision,
        turn_id=turn_id,
        start=start,
        user=user,
        time=T4,
    ).model_copy(update={"task_id": task_id})
    revision = api.begin_turn(report.revision, turn)
    if fenced:
        revision = api.fence_tool(
            revision,
            fence(report.binding, turn_id=turn_id, index=start + 1).model_copy(
                update={"observed_at_utc": T4}
            ),
        )
    if effect_path is not None:
        with effect_path.open("ab") as effect:
            effect.write(b"effect\n")
            effect.flush()
            os.fsync(effect.fileno())
    history = restore_messages(report.checkpoint) + messages(user)
    return api.commit_turn(revision, settled(turn, history=history, outcome=outcome))


def task_crash_writer(
    root_text: str,
    session_id: str,
    window: Literal[
        "running", "fenced", "checkpoint", "turn_committed", "inspecting", "update"
    ],
    barrier: Connection,
) -> None:
    root = Path(root_text)
    api = store(root)
    session = api.inspect(session_id)
    task = api.create_task(session.revision, task_id=TASK, input_digest=INPUT_DIGEST)
    effect_path = root / "work" / "effect.log"
    if window == "fenced":
        turn = intent(session.binding, session.revision, time=T4).model_copy(
            update={"task_id": TASK}
        )
        revision = api.begin_turn(session.revision, turn)
        api.fence_tool(
            revision,
            fence(session.binding).model_copy(update={"observed_at_utc": T4}),
        )
        with effect_path.open("ab") as effect:
            effect.write(b"effect\n")
            effect.flush()
            os.fsync(effect.fileno())
    elif window != "running":
        receipt = commit_task_checkpoint(
            api, task, fenced=True, effect_path=effect_path
        )
        if window != "checkpoint":
            task = api.commit_task_turn(task.revision, receipt)
            if window in ("inspecting", "update"):
                api.start_task_inspection(task.revision)
    barrier.send(window)
    barrier.recv()


def task_crash_at(root: Path, session_id: str, window: str) -> None:
    """Kill a task writer at a named window, blocking inside PostgreSQL if needed."""
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    database = database_for(root)
    key = _barrier_key(root)
    # The checkpoint window crashes after its commit, like the old barrier did.
    table = {"update": "tasks"}.get(window)
    blocker = None
    if table is not None:
        _install_barrier(
            database,
            key,
            table,
            events="UPDATE",
            when="WHEN (OLD.phase <> 'inspecting' AND NEW.phase = 'inspecting')",
        )
        blocker = database.dedicated("agent")
        blocker.execute("SELECT pg_advisory_lock(%s)", (key,))
    worker = context.Process(
        target=task_crash_writer, args=(str(root), session_id, window, child)
    )
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
