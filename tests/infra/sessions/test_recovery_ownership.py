import asyncio
import os
import sqlite3
from pathlib import Path
from typing import Literal

import pytest

from cairn.infra.sessions.models import (
    HostResolution,
    SessionStoreError,
    VersionConflict,
)
from cairn.runtime.lifecycle import await_owned, settle_owned
from tests.support.sessions import (
    OTHER_TURN,
    SESSION,
    T4,
    TURN,
    assert_competing_writer,
    binding,
    fence,
    intent,
    settled,
    store,
)


def test_rejected_live_reconciliation_keeps_turn_executable(tmp_path: Path) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    try:
        initial = api.create(bind).revision
        turn = intent(bind, initial)
        active = api.begin_turn(initial, turn)
        report = api.inspect(SESSION)
        for _ in range(2):
            with pytest.raises(SessionStoreError, match="turn_still_owned"):
                api.release_turn(active, TURN)
            assert api.inspect(SESSION) == report
        with pytest.raises(SessionStoreError, match="turn_still_owned"):
            api.reconcile(
                active,
                HostResolution(
                    session_id=SESSION,
                    turn_id=TURN,
                    actor="host",
                    resolved_at_utc=T4,
                    binding=bind,
                ),
            )
        with pytest.raises(SessionStoreError, match="turn_still_owned"):
            api.close()
        assert api.inspect(SESSION) == report
        assert not report.local_fault
        assert api.commit_turn(active, settled(turn)).revision.head_seq == 1
    finally:
        api.close()
    api.close()  # ordinary idempotent close after terminal commit
    assert_competing_writer(tmp_path, "writer_acquired")


@pytest.mark.parametrize("fault", [False, True, "storage"])
def test_inspect_reconcile_cannot_bypass_work_or_repeated_cancel_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: bool | Literal["storage"]
) -> None:
    async def scenario() -> None:
        bind = binding(tmp_path)
        api = store(tmp_path)
        initial = api.create(bind).revision
        turn = intent(bind, initial)
        active = api.fence_tool(api.begin_turn(initial, turn), fence(bind))
        entered, cleanup_entered, cleanup_release = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        observations: list[str] = []

        async def cleanup() -> None:
            cleanup_entered.set()
            await cleanup_release.wait()
            observations.append("cleaned")

        async def work() -> None:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await await_owned(asyncio.create_task(cleanup()))

        worker = asyncio.create_task(work())
        observer: asyncio.Task[object] | None = None
        try:
            await entered.wait()
            if fault == "storage":

                def failed_commit(conn: sqlite3.Connection) -> None:
                    raise sqlite3.OperationalError("opaque storage failure")

                with monkeypatch.context() as injection:
                    injection.setattr(api, "_commit", failed_commit)
                    with pytest.raises(SessionStoreError, match="commit_uncertain"):
                        api.fence_tool(active, fence(bind, ordinal=2, call_id="call-2"))
            elif fault:
                active = api.require_recovery(active, "finalization_failed")

            def reject_recovery() -> None:
                before = api.inspect(SESSION)
                connection = api._connection
                ownership = api._owned_turns.copy()
                inspected = api._inspected.copy()
                descriptors = (api._paths.lock_fd, api._paths.access_fd)
                identities = tuple(
                    (os.fstat(fd).st_dev, os.fstat(fd).st_ino) for fd in descriptors
                )
                with pytest.raises(
                    SessionStoreError, match=r"^Session store: turn_still_owned$"
                ):
                    api.close()
                assert api._connection is connection and connection is not None
                assert api._owned_turns == ownership
                assert api._inspected == inspected
                assert (api._paths.lock_fd, api._paths.access_fd) == descriptors
                assert (
                    tuple(
                        (os.fstat(fd).st_dev, os.fstat(fd).st_ino) for fd in descriptors
                    )
                    == identities
                )
                assert api.inspect(SESSION) == before  # no new fault or row mutation
                assert_competing_writer(tmp_path, "writer_busy")
                with pytest.raises(SessionStoreError, match="turn_still_owned"):
                    api.reconcile(
                        before.revision,
                        HostResolution(
                            session_id=SESSION,
                            turn_id=TURN,
                            actor="host",
                            resolved_at_utc=T4,
                            binding=bind,
                        ),
                    )
                assert api.inspect(SESSION) == before
                assert before.revision == active
                assert before.checkpoint.seq == 0
                assert before.unresolved is not None
                assert before.unresolved.diagnostics.fences == (fence(bind),)
                assert observations == []

            reject_recovery()  # tool/owned work is still running
            observer = asyncio.create_task(settle_owned(worker))
            await cleanup_entered.wait()
            for _ in range(2):
                observer.cancel()
                await asyncio.sleep(0)  # deliver cancellation at the held barrier
                assert not observer.done()
                reject_recovery()  # SQL recovery-required is not settlement proof
            with pytest.raises(SessionStoreError):
                api.begin_turn(active, intent(bind, active, turn_id=OTHER_TURN))
            assert api.inspect(SESSION).revision == active
            cleanup_release.set()
            await observer
            assert worker.done() and observations == ["cleaned"]
            with pytest.raises(SessionStoreError, match="turn_still_owned"):
                api.close()  # finished cleanup alone has not released ownership
            # The future runtime boundary calls this only after owned settlement.
            api.release_turn(active, TURN)
            report = api.inspect(SESSION)
            assert report.local_fault  # releasing ownership does not clear the fault
            api.close()  # close is safe now, before any host reconciliation
            api.close()
            assert_competing_writer(tmp_path, "writer_acquired")
            api = store(tmp_path)
            reopened = api.inspect(SESSION)
            assert reopened.revision == report.revision
            assert reopened.checkpoint == report.checkpoint
            assert reopened.attempts == report.attempts
            report = reopened
            ready = api.reconcile(
                report.revision,
                HostResolution(
                    session_id=SESSION,
                    turn_id=TURN,
                    actor="host",
                    resolved_at_utc=T4,
                    binding=bind,
                ),
            )
            report = api.inspect(SESSION)
            assert report.attempts[0].state == "abandoned"
            assert report.attempts[0].diagnostics.fences == (fence(bind),)
            assert report.checkpoint.messages == ()
            next_turn = intent(bind, ready, turn_id=OTHER_TURN, time=T4)
            next_active = api.begin_turn(ready, next_turn)
            api.commit_turn(
                next_active, settled(next_turn, history=[], outcome="cancelled")
            )  # the admission control created no external work
        finally:
            cleanup_release.set()
            await settle_owned(worker)
            if observer is not None:
                await await_owned(observer)
            api.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("committed", [False, True])
def test_uncertain_commit_keeps_ownership_until_exact_faulted_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, committed: bool
) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    try:
        initial = api.create(bind).revision
        turn = intent(bind, initial)
        active = api.begin_turn(initial, turn)
        original_commit = api._commit

        def uncertain(conn: sqlite3.Connection) -> None:
            if committed:
                conn.commit()
            raise sqlite3.OperationalError("uncertain")

        monkeypatch.setattr(api, "_commit", uncertain)
        with pytest.raises(SessionStoreError, match="commit_uncertain"):
            api.commit_turn(active, settled(turn))
        monkeypatch.setattr(api, "_commit", original_commit)
        report = api.inspect(SESSION)
        resolution = HostResolution(
            session_id=SESSION,
            turn_id=None if committed else TURN,
            actor="host",
            resolved_at_utc=T4,
            binding=report.binding,
        )
        with pytest.raises(SessionStoreError, match="turn_still_owned"):
            api.reconcile(report.revision, resolution)
        with pytest.raises(SessionStoreError, match="turn_still_owned"):
            api.close()  # neither old nor committed SQL proves owned cleanup ended
        assert api.inspect(SESSION) == report
        with pytest.raises(VersionConflict):
            api.release_turn(initial, TURN)
        with pytest.raises(SessionStoreError, match="live_turn_required"):
            api.release_turn(report.revision, OTHER_TURN)
        assert api.inspect(SESSION) == report
        api.release_turn(report.revision, TURN)
        with pytest.raises(SessionStoreError, match="fresh_inspection_required"):
            api.reconcile(report.revision, resolution)
        released = api.inspect(SESSION)
        assert released == report  # release is local; no rows/revisions changed
        ready = api.reconcile(released.revision, resolution)
        reconciled = api.inspect(SESSION)
        assert ready.head_seq == int(committed)
        assert reconciled.receipt == report.receipt
        assert reconciled.attempts[0].state == (
            "completed" if committed else "abandoned"
        )
    finally:
        api.close()
