"""SIGKILL tests application crashes, not disk/power-loss durability."""

import multiprocessing
import os
from pathlib import Path

import pytest

from cairn.infra.sessions.models import HostResolution, SessionStoreError
from tests.support.sessions import (
    SESSION,
    T4,
    binding,
    competing_writer,
    crash_writer,
    fence,
    intent,
    store,
)


@pytest.mark.parametrize(
    "window",
    [
        "before_intent",
        "intent",
        "fence",
        "checkpoint_insert",
        "attempt_update",
        "before_commit",
        "committed",
    ],
)
def test_crash_barriers_restore_old_or_complete_head_without_replay(
    tmp_path: Path, window: str
) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    api.create(bind)
    api.close()
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    worker = context.Process(target=crash_writer, args=(str(tmp_path), window, child))
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
    # Killing the owner releases flock; no PID/lease expiry or unlink required.
    api = store(tmp_path)
    try:
        report = api.inspect(SESSION)
        expected_seq = int(window == "committed")
        assert report.checkpoint.seq == expected_seq
        effect = tmp_path / "work" / "effect.txt"
        before = effect.read_bytes() if effect.exists() else None
        assert before == (
            b"one effect\n"
            if window
            in ("checkpoint_insert", "attempt_update", "before_commit", "committed")
            else None
        )
        if window == "before_intent":
            assert report.status == "clean" and report.attempts == ()
        elif window == "committed":
            assert report.status == "clean"
            assert report.receipt.turn_id == report.attempts[0].intent.turn_id
            assert report.attempts[0].state == "completed"
        else:
            assert report.unresolved is not None
            assert report.status == (
                "executing" if window == "intent" else "effects_unknown"
            )
            assert report.checkpoint.messages == ()
            with pytest.raises(SessionStoreError, match="live_turn_required"):
                api.fence_tool(
                    report.revision,
                    fence(
                        bind, ordinal=1 if window == "intent" else 2, call_id="new-call"
                    ),
                )
            report = api.inspect(SESSION)
            ready = api.reconcile(
                report.revision,
                HostResolution(
                    session_id=SESSION,
                    turn_id=report.unresolved.intent.turn_id
                    if report.unresolved
                    else None,
                    actor="test host",
                    resolved_at_utc=T4,
                    binding=bind,
                ),
            )
            assert ready.head_seq == 0
            abandoned = api.inspect(SESSION)
            assert abandoned.attempts[0].state == "abandoned"
            assert abandoned.checkpoint.messages == ()
        for _ in range(2):
            assert api.inspect(SESSION).checkpoint.seq == expected_seq
        assert (effect.read_bytes() if effect.exists() else None) == before
    finally:
        api.close()


def test_store_wide_writer_lock_denies_another_session_and_reader_does_not_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    try:
        initial = api.create(bind).revision
        api.begin_turn(initial, intent(bind, initial))
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        worker = context.Process(target=competing_writer, args=(str(tmp_path), child))
        worker.start()
        child.close()
        try:
            assert parent.poll(15)
            assert parent.recv() == "writer_busy"
        finally:
            worker.join(10)
            if worker.is_alive():
                worker.kill()
                worker.join(10)
            parent.close()
        assert worker.exitcode == 0
        import fcntl

        def forbidden_lock(fd: int, operation: int) -> None:
            raise AssertionError("reader acquired writer lock")

        monkeypatch.setattr(fcntl, "flock", forbidden_lock)
        original_open = os.open

        def reader_open(
            file: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            assert Path(os.fsdecode(file)).name != api.path.name, (
                "extra raw DB fd could drop SQLite locks"
            )
            return original_open(file, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(os, "open", reader_open)
        reader = store(tmp_path, mode="inspect")
        try:
            report = reader.inspect(SESSION)
            assert report.status == "executing"  # never declares a live writer crashed
            assert report.revision.version == 1
            with pytest.raises(SessionStoreError, match="read_only_store"):
                reader.begin_turn(report.revision, intent(bind, report.revision))
            assert api.inspect(SESSION).revision == report.revision
        finally:
            reader.close()
    finally:
        api.close()
