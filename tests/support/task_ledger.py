"""Offline Task/checkpoint facts and native process-death barriers."""

import hashlib
import os
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Literal

from cairn.infra.sessions.codec import restore_messages
from cairn.infra.sessions.models import CheckpointReceipt, Outcome
from cairn.infra.sessions.store import SessionStore
from cairn.infra.sessions.task_models import TaskRecord
from tests.support.sessions import T4, TURN, fence, intent, messages, settled, store

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
            if window == "update":
                conn = api._conn()

                def stop(sql: str) -> None:
                    if sql.startswith("UPDATE tasks"):
                        barrier.send("update")
                        barrier.recv()

                conn.set_trace_callback(stop)
            if window in ("inspecting", "update"):
                api.start_task_inspection(task.revision)
    barrier.send(window)
    barrier.recv()
