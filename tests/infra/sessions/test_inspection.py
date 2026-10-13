import multiprocessing
import sqlite3
import stat
from pathlib import Path
from threading import Thread

import pytest

from cairn.infra.sessions.models import SessionStoreError
from cairn.infra.sessions.store import SessionStore
from tests.support.sessions import (
    SESSION,
    binding,
    fence,
    inspection_writer,
    intent,
    settled,
    store,
)


def filesystem_state(directory: Path) -> dict[str, tuple[bytes, tuple[int, ...]]]:
    result: dict[str, tuple[bytes, tuple[int, ...]]] = {}
    for path in directory.iterdir():
        info = path.stat()
        result[path.name] = (
            path.read_bytes(),
            (
                info.st_dev,
                info.st_ino,
                stat.S_IMODE(info.st_mode),
                info.st_uid,
                info.st_gid,
                info.st_nlink,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            ),
        )
    return result


@pytest.mark.parametrize("live", [False, True])
def test_inspection_changes_no_database_or_sidecar_files(
    tmp_path: Path, live: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    initial = api.create(bind).revision
    turn = intent(bind, initial)
    active = api.fence_tool(api.begin_turn(initial, turn), fence(bind))
    if not live:
        active = api.commit_turn(active, settled(turn)).revision
        api.close()
        assert not Path(str(api.path) + "-wal").exists()
        assert not Path(str(api.path) + "-shm").exists()
    before = filesystem_state(api.directory)
    original_schema = SessionStore._schema

    def checked_schema(self: SessionStore, conn: sqlite3.Connection) -> None:
        assert self.mode == "inspect"
        assert conn.getconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE)
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE sessions SET version=version+1")
        # Also observe the connection while open, so create-then-delete cannot
        # masquerade as a nonmutating read at the after-close boundary.
        assert filesystem_state(api.directory) == before
        original_schema(self, conn)

    monkeypatch.setattr(SessionStore, "_schema", checked_schema)
    try:
        reader = store(tmp_path, mode="inspect")
        try:
            for _ in range(2):
                report = reader.inspect(SESSION)
                assert report.revision == active
                assert report.checkpoint.seq == int(not live)
                assert report.attempts[0].diagnostics.fences == (fence(bind),)
                assert report.status == ("effects_unknown" if live else "clean")
                assert filesystem_state(api.directory) == before
        finally:
            reader.close()
        assert filesystem_state(api.directory) == before
    finally:
        api.close()


def test_reader_rejects_inflight_sql_and_sees_committed_wal_then_cold_head(
    tmp_path: Path,
) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    api.create(bind)
    api.close()
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    worker = context.Process(target=inspection_writer, args=(str(tmp_path), child))
    worker.start()
    child.close()
    reader = None
    try:
        assert parent.poll(15) and parent.recv() == ("active", 2)
        reader = store(tmp_path, mode="inspect")
        report = reader.inspect(SESSION)
        assert report.revision.version == 2 and report.status == "effects_unknown"
        parent.send("commit")
        assert parent.poll(15) and parent.recv() == "before_commit"
        before = filesystem_state(reader.directory)
        with pytest.raises(SessionStoreError, match="store_busy"):
            reader.inspect(SESSION)
        with pytest.raises(SessionStoreError, match="store_busy"):
            store(tmp_path, mode="inspect")
        assert filesystem_state(reader.directory) == before
        parent.send("release")
        assert parent.poll(15) and parent.recv() == ("committed", 3)
        before = filesystem_state(reader.directory)
        committed = reader.inspect(SESSION)
        assert committed.revision.version == 3
        assert committed.receipt.turn_id == report.attempts[0].intent.turn_id
        assert committed.checkpoint.seq == 1 and committed.status == "clean"
        assert filesystem_state(reader.directory) == before
        parent.send("close")
        assert parent.poll(15) and parent.recv() == "closed"
        worker.join(10)
        assert worker.exitcode == 0
        before = filesystem_state(reader.directory)
        assert reader.inspect(SESSION) == committed
        assert filesystem_state(reader.directory) == before
    finally:
        if reader is not None:
            reader.close()
        if worker.is_alive():
            worker.kill()
        worker.join(10)
        parent.close()


@pytest.mark.parametrize("operation", ["fence", "close_attempt"])
def test_shared_inspection_guard_prevents_concurrent_writer_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    api.create(bind)
    api.close()
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    worker = context.Process(target=inspection_writer, args=(str(tmp_path), child))
    worker.start()
    child.close()
    reader = None
    try:
        assert parent.poll(15) and parent.recv() == ("active", 2)
        reader = store(tmp_path, mode="inspect")
        original_schema = reader._schema
        requested = False

        def held_schema(conn: sqlite3.Connection) -> None:
            nonlocal requested
            if not requested:
                requested = True
                parent.send(operation)
                assert parent.poll(15) and parent.recv() == "store_busy"
            original_schema(conn)

        monkeypatch.setattr(reader, "_schema", held_schema)
        before = filesystem_state(reader.directory)
        report = reader.inspect(SESSION)
        assert report.revision.version == 2
        assert report.attempts[0].diagnostics.fences == (fence(bind),)
        assert filesystem_state(reader.directory) == before
        parent.send("close")
        assert parent.poll(15) and parent.recv() == "closed"
        worker.join(10)
        assert worker.exitcode == 0
    finally:
        if reader is not None:
            reader.close()
        if worker.is_alive():
            worker.kill()
        worker.join(10)
        parent.close()


def test_unavailable_readonly_vfs_never_falls_back_or_changes_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    api.create(bind)
    before = filesystem_state(api.directory)

    def unavailable(database: str, **options: object) -> sqlite3.Connection:
        assert database.endswith("?mode=ro&vfs=unix-none")
        raise sqlite3.OperationalError("unsupported VFS")

    monkeypatch.setattr(sqlite3, "connect", unavailable)
    with pytest.raises(SessionStoreError, match="storage_unavailable"):
        store(tmp_path, mode="inspect")
    assert filesystem_state(api.directory) == before
    api.close()


def test_missing_read_guard_requires_explicit_writer_open_without_read_side_effects(
    tmp_path: Path,
) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    api.create(bind)
    api.close()
    (api.directory / "inspection.lock").unlink()
    before = filesystem_state(api.directory)
    with pytest.raises(SessionStoreError, match="unsafe_path_or_lock"):
        store(tmp_path, mode="inspect")
    assert filesystem_state(api.directory) == before
    writer = store(tmp_path)  # an explicitly writable host operation establishes it
    writer.close()
    before = filesystem_state(api.directory)
    reader = store(tmp_path, mode="inspect")
    try:
        assert reader.inspect(SESSION).revision.version == 0
        assert filesystem_state(api.directory) == before
    finally:
        reader.close()


def test_missing_close_configuration_and_rollback_journal_fail_without_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    api.create(bind)
    api.close()
    before = filesystem_state(api.directory)
    monkeypatch.delattr(sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE")
    with pytest.raises(SessionStoreError, match="storage_unavailable"):
        store(tmp_path, mode="inspect")
    assert filesystem_state(api.directory) == before
    monkeypatch.undo()
    journal = Path(str(api.path) + "-journal")
    journal.touch(mode=0o600)
    journal.write_bytes(b"unknown recovery journal")
    before = filesystem_state(api.directory)
    with pytest.raises(SessionStoreError, match="inspection_unavailable"):
        store(tmp_path, mode="inspect")
    assert filesystem_state(api.directory) == before


def test_inspector_cannot_cross_threads_or_release_another_read_guard(
    tmp_path: Path,
) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    api.create(bind)
    api.close()
    reader = store(tmp_path, mode="inspect")
    errors: list[str] = []

    def transferred() -> None:
        try:
            reader.inspect(SESSION)
        except SessionStoreError as error:
            errors.append(error.code)
        try:
            reader.close()
        except SessionStoreError as error:
            errors.append(error.code)

    try:
        before = filesystem_state(reader.directory)
        worker = Thread(target=transferred)
        worker.start()
        worker.join(5)
        assert not worker.is_alive() and errors == ["wrong_store_thread"] * 2
        assert reader.inspect(SESSION).status == "clean"
        assert filesystem_state(reader.directory) == before
    finally:
        reader.close()
