import hashlib
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from cairn.infra.sessions.codec import canonical
from cairn.infra.sessions.models import (
    MAX_DATABASE_BYTES,
    HostResolution,
    SessionStoreError,
)
from cairn.infra.sessions.store import SessionStore, sqlite_wal_fix_supported
from tests.support.sessions import (
    OTHER_TURN,
    SESSION,
    T4,
    binding,
    intent,
    settled,
    store,
)


@pytest.mark.parametrize("root_name", ["work", "tmp", "cache", "extra"])
def test_every_effective_writable_root_is_forbidden(
    tmp_path: Path, root_name: str
) -> None:
    root = tmp_path.resolve() / root_name
    with pytest.raises(SessionStoreError, match="overlap"):
        SessionStore(
            root / "sessions",
            forbidden_roots=tuple(
                tmp_path.resolve() / r for r in ("work", "tmp", "cache", "extra")
            ),
        )
    assert not root.exists()
    with pytest.raises(SessionStoreError, match="forbidden_roots"):
        SessionStore(tmp_path.resolve() / "store", forbidden_roots=())


@pytest.mark.parametrize(
    "name",
    [
        "directory",
        "parent",
        "sessions.sqlite3",
        "sessions.sqlite3-wal",
        "sessions.sqlite3-shm",
        "sessions.sqlite3-journal",
        "writer.lock",
        "inspection.lock",
    ],
)
def test_symlink_components_and_all_store_files_fail_closed(
    tmp_path: Path, name: str
) -> None:
    root = tmp_path.resolve()
    target = root / "target"
    target.mkdir(mode=0o700)
    directory = root / "store"
    if name in ("directory", "parent"):
        directory.symlink_to(target)
        if name == "parent":
            directory /= "nested"
    else:
        directory.mkdir(mode=0o700)
        target_file = target / "untouched"
        target_file.write_bytes(b"untouched")
        (directory / name).symlink_to(target_file)
    with pytest.raises(SessionStoreError):
        SessionStore(directory, forbidden_roots=(root / "work",))
    if name not in ("directory", "parent"):
        assert (target / "untouched").read_bytes() == b"untouched"


@pytest.mark.parametrize(
    "name",
    [
        "directory",
        "parent",
        "database",
        "lock",
        "read_lock",
        "wal",
        "shm",
        "hardlink",
        "mode",
    ],
)
def test_replaced_path_descriptor_or_permissions_prevents_mutation(
    tmp_path: Path, name: str
) -> None:
    bind = binding(tmp_path)
    root = tmp_path.resolve()
    holder = root / "holder"
    api = SessionStore(holder / "store", forbidden_roots=(root / "work",))
    try:
        initial = api.create(bind).revision
        if name == "directory":
            api.directory.rename(holder / "saved")
            api.directory.mkdir(mode=0o700)
        elif name == "parent":
            holder.rename(root / "saved-parent")
            (holder / "store").mkdir(parents=True, mode=0o700)
        elif name == "mode":
            api.path.chmod(0o644)
        elif name == "hardlink":
            os.link(api.path, root / "db-alias")
        else:
            path = {
                "database": api.path,
                "lock": api.directory / "writer.lock",
                "read_lock": api.directory / "inspection.lock",
                "wal": Path(str(api.path) + "-wal"),
                "shm": Path(str(api.path) + "-shm"),
            }[name]
            path.rename(path.with_name(path.name + ".retained"))
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        with pytest.raises(SessionStoreError):
            api.begin_turn(initial, intent(bind, initial))
        # We fail before SQL changes, even if the original connection points at
        # a moved DB. The host retains both the original and replacement files.
        assert api._connection is not None
        assert (
            api._connection.execute("SELECT version FROM sessions").fetchone()[0] == 0
        )
    finally:
        api.close()


def test_private_modes_stable_lock_and_first_create_directory_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bind = binding(tmp_path)
    synced: list[int] = []
    original = os.fsync

    def sync(fd: int) -> None:
        synced.append(os.fstat(fd).st_mode)
        original(fd)

    monkeypatch.setattr(os, "fsync", sync)
    api = store(tmp_path)
    try:
        api.create(bind)
        assert stat.S_IMODE(api.directory.stat().st_mode) == 0o700
        for path in api.directory.iterdir():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert any(stat.S_ISDIR(mode) for mode in synced)
        assert any(stat.S_ISREG(mode) for mode in synced)
        inode = (api.directory / "writer.lock").stat().st_ino
    finally:
        api.close()
    api = store(tmp_path)
    try:
        assert (api.directory / "writer.lock").stat().st_ino == inode
    finally:
        api.close()


def test_redirected_forbidden_root_is_rechecked_before_mutation(tmp_path: Path) -> None:
    bind = binding(tmp_path)
    root = tmp_path.resolve()
    alias = root / "cache-alias"
    alias.symlink_to(root / "cache")
    api = SessionStore(root / "store", forbidden_roots=(root / "work", alias))
    try:
        initial = api.create(bind).revision
        alias.unlink()
        alias.symlink_to(api.directory)
        with pytest.raises(SessionStoreError, match="forbidden_root_changed"):
            api.begin_turn(initial, intent(bind, initial))
        assert api._connection is not None
        assert (
            api._connection.execute("SELECT version FROM sessions").fetchone()[0] == 0
        )
    finally:
        api.close()


def test_first_create_sync_failure_does_not_acknowledge_or_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    try:

        def failure(fd: int) -> None:
            raise OSError("sensitive filesystem exception")

        monkeypatch.setattr(os, "fsync", failure)
        with pytest.raises(SessionStoreError, match="acknowledgement_uncertain"):
            api.create(bind)
        report = api.inspect(SESSION)
        assert report.local_fault
        assert report.checkpoint.seq == 0
        with pytest.raises(SessionStoreError, match="recovery_required"):
            api.begin_turn(report.revision, intent(bind, report.revision))
    finally:
        api.close()


def test_missing_inspection_never_creates_paths_or_schema(tmp_path: Path) -> None:
    with pytest.raises(SessionStoreError):
        store(tmp_path, mode="inspect")
    assert not (tmp_path / "store").exists()
    directory = tmp_path.resolve() / "store"
    directory.mkdir(mode=0o700)
    directory.joinpath("sessions.sqlite3").touch(mode=0o600)
    before = directory.joinpath("sessions.sqlite3").read_bytes()
    with pytest.raises(SessionStoreError, match="unsafe_path_or_lock"):
        store(tmp_path, mode="inspect")
    assert sorted(p.name for p in directory.iterdir()) == ["sessions.sqlite3"]
    # An established read guard must still reject the empty/unsupported schema.
    directory.joinpath("inspection.lock").touch(mode=0o600)
    with pytest.raises(SessionStoreError, match="schema_mismatch"):
        store(tmp_path, mode="inspect")
    assert directory.joinpath("sessions.sqlite3").read_bytes() == before
    assert not directory.joinpath("writer.lock").exists()


@pytest.mark.parametrize(
    "damage",
    [
        "schema_version",
        "schema_shape",
        "json",
        "digest",
        "missing_head",
        "foreign_key",
        "column_seq",
        "extra",
        "duplicate",
        "format",
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
    path = api.path
    api.close()
    with sqlite3.connect(path) as conn:
        if damage == "schema_version":
            conn.execute("PRAGMA user_version=99")
        elif damage == "schema_shape":
            conn.execute("CREATE TABLE unexpected (x)")
        elif damage == "missing_head":
            conn.execute("DELETE FROM checkpoints WHERE seq=1")
        elif damage == "foreign_key":
            conn.execute("UPDATE turns SET base_seq=999")
        elif damage == "column_seq":
            conn.execute("UPDATE sessions SET head_seq=0")
        elif damage in ("intent", "fence", "abandoned_commit"):
            selected_turn = OTHER_TURN if damage == "abandoned_commit" else turn.turn_id
            row = conn.execute(
                "SELECT diagnostics_json FROM turns WHERE turn_id=?", (selected_turn,)
            ).fetchone()[0]
            import json

            data = json.loads(row)
            if damage == "intent":
                data["message_start"] = 9
            elif damage == "fence":
                data["fences"] = [{"unexpected": "field"}]
            else:
                data["commit_version"] = 123
            conn.execute(
                "UPDATE turns SET diagnostics_json=? WHERE turn_id=?",
                (canonical(data).decode(), selected_turn),
            )
        else:
            row = conn.execute(
                "SELECT payload FROM checkpoints WHERE seq=1"
            ).fetchone()[0]
            if damage == "json":
                row = b"{"
            elif damage == "digest":
                conn.execute("UPDATE checkpoints SET sha256=? WHERE seq=1", ("0" * 64,))
            elif damage == "extra":
                row = row[:-1] + b',"extra":true}'
            elif damage == "duplicate":
                row = row[:-1] + b',"seq":1}'
            elif damage == "format":
                row = row.replace(b'"format_version":2', b'"format_version":99')
            if damage != "digest":
                conn.execute(
                    "UPDATE checkpoints SET payload=?,sha256=? WHERE seq=1",
                    (row, hashlib.sha256(row).hexdigest()),
                )
    before = path.read_bytes()
    reader: SessionStore | None = None
    try:
        with pytest.raises(SessionStoreError):
            reader = store(tmp_path, mode="inspect")
            reader.inspect(SESSION)
    finally:
        if reader is not None:
            reader.close()
    assert path.exists() and path.read_bytes() == before


def test_database_sparse_file_boundary_and_release_gate(tmp_path: Path) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    api.create(bind)
    path = api.path
    api.close()
    os.truncate(path, MAX_DATABASE_BYTES)
    api = store(tmp_path, mode="inspect")
    try:
        assert api.inspect(SESSION).checkpoint.seq == 0
    finally:
        api.close()
    os.truncate(path, MAX_DATABASE_BYTES + 1)
    with pytest.raises(SessionStoreError, match="database_limit"):
        store(tmp_path, mode="inspect")
    assert path.stat().st_size == MAX_DATABASE_BYTES + 1
    assert not sqlite_wal_fix_supported((3, 50, 4))
    assert sqlite_wal_fix_supported((3, 50, 7))
    assert sqlite_wal_fix_supported((3, 44, 6))
    assert sqlite_wal_fix_supported((3, 51, 3))
    assert not sqlite_wal_fix_supported((3, 51, 2))


def test_unsupported_lock_and_filesystem_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    def broken(fd: int, operation: int) -> None:
        raise OSError("arbitrary unsafe lock failure")

    monkeypatch.setattr(fcntl, "flock", broken)
    with pytest.raises(SessionStoreError, match="unsupported_locking"):
        store(tmp_path)
    assert not (tmp_path / "store" / "sessions.sqlite3").exists()
    monkeypatch.undo()

    def unsupported_filesystem(fd: int) -> None:
        raise SessionStoreError("unsupported_filesystem")

    monkeypatch.setattr(
        "cairn.infra.sessions.paths._local_filesystem", unsupported_filesystem
    )
    with pytest.raises(SessionStoreError, match="unsupported_filesystem"):
        store(tmp_path)
