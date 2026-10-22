"""Concrete, host-only SQLite store. Facts in this database never dispatch work."""

import hashlib
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from cairn.infra.sessions.codec import (
    canonical,
    decode_checkpoint,
    decode_record,
    decode_storage_checkpoint,
    encode_checkpoint,
    encode_record,
    encode_storage_checkpoint,
)
from cairn.infra.sessions.ledger import TaskLedgerMixin
from cairn.infra.sessions.models import (
    MAX_CHECKPOINT_BYTES,
    MAX_DATABASE_BYTES,
    MAX_INTEGER,
    MAX_MESSAGES,
    MAX_RECORD_BYTES,
    MAX_WAL_BYTES,
    SCHEMA_VERSION,
    AttemptDiagnostics,
    CheckpointDelta,
    CheckpointPayload,
    CheckpointReceipt,
    HostResolution,
    MessageRange,
    Reason,
    RecoveryReport,
    Revision,
    SessionBinding,
    SessionState,
    SessionStoreError,
    SettledTurn,
    StoredMessage,
    StrictDTO,
    ToolFence,
    TurnIntent,
    TurnRecord,
    VersionConflict,
    instant,
    same_workspace,
    utc_text,
    uuid4_text,
)
from cairn.infra.sessions.paths import StorePaths
from cairn.infra.sessions.task_models import MAX_TASK_BYTES, TaskRevision
from cairn.infra.sqlite_policy import (
    UNVERIFIED_SQLITE_DECLARATION as UNVERIFIED_SQLITE_DECLARATION,
)
from cairn.infra.sqlite_policy import (
    VERIFIED_SQLITE_VERSIONS as VERIFIED_SQLITE_VERSIONS,
)

# The WAL-reset judgement lives in one neutral module so Project Memory can
# share it without importing this package. Re-exported explicitly (``X as X``)
# because this module has long been the documented import site for the session
# store's release gate, and existing callers and tests import it from here.
from cairn.infra.sqlite_policy import (
    AdmissionOptionError as AdmissionOptionError,
)
from cairn.infra.sqlite_policy import (
    SqliteAdmission as SqliteAdmission,
)
from cairn.infra.sqlite_policy import (
    sqlite_wal_fix_supported as sqlite_wal_fix_supported,
)
from cairn.infra.sqlite_policy import (
    unverified_declared as unverified_declared,
)
from cairn.infra.sqlite_policy import (
    wal_admission as _policy_wal_admission,
)


def wal_admission(
    *, allow_unverified: bool = False, version: tuple[int, int, int] | None = None
) -> SqliteAdmission:
    """Session-store view of the shared policy.

    Reuses the one version judgement while keeping this store's established error
    contract: a non-boolean option is ``invalid_options`` here, not the policy's
    neutral option error.
    """
    try:
        return _policy_wal_admission(allow_unverified=allow_unverified, version=version)
    except AdmissionOptionError:
        raise SessionStoreError("invalid_options") from None


_SCHEMA = (
    """CREATE TABLE sessions (
        session_id TEXT PRIMARY KEY,
        version INTEGER NOT NULL CHECK(version >= 0),
        head_seq INTEGER NOT NULL CHECK(head_seq >= 0),
        state TEXT NOT NULL CHECK(state IN ('ready','executing','recovery_required')),
        active_turn_id TEXT,
        binding_json TEXT NOT NULL,
        created_at_utc TEXT NOT NULL,
        updated_at_utc TEXT NOT NULL,
        CHECK((state='ready' AND active_turn_id IS NULL)
            OR (state='executing' AND active_turn_id IS NOT NULL)
            OR state='recovery_required'),
        FOREIGN KEY(session_id,head_seq) REFERENCES checkpoints(session_id,seq)
            DEFERRABLE INITIALLY DEFERRED,
        FOREIGN KEY(session_id,active_turn_id) REFERENCES turns(session_id,turn_id)
            DEFERRABLE INITIALLY DEFERRED
    )""",
    """CREATE TABLE turns (
        session_id TEXT NOT NULL REFERENCES sessions(session_id),
        turn_id TEXT NOT NULL,
        base_seq INTEGER NOT NULL,
        task_id TEXT,
        state TEXT NOT NULL CHECK(state IN
            ('executing','completed','interrupted','recovery_required','abandoned')),
        input_digest TEXT NOT NULL,
        started_at_utc TEXT NOT NULL,
        ended_at_utc TEXT,
        settled_seq INTEGER,
        diagnostics_json TEXT NOT NULL,
        PRIMARY KEY(session_id,turn_id),
        CHECK((state IN ('completed','interrupted') AND settled_seq IS NOT NULL)
            OR (state IN ('executing','recovery_required','abandoned') AND settled_seq IS NULL)),
        FOREIGN KEY(session_id,base_seq) REFERENCES checkpoints(session_id,seq),
        FOREIGN KEY(session_id,settled_seq) REFERENCES checkpoints(session_id,seq)
            DEFERRABLE INITIALLY DEFERRED
    )""",
    """CREATE UNIQUE INDEX one_unresolved_turn ON turns(session_id)
        WHERE state IN ('executing','recovery_required')""",
    """CREATE TABLE checkpoints (
        session_id TEXT NOT NULL REFERENCES sessions(session_id),
        seq INTEGER NOT NULL CHECK(seq >= 0),
        turn_id TEXT,
        format_version INTEGER NOT NULL CHECK(format_version=1),
        created_at_utc TEXT NOT NULL,
        payload BLOB NOT NULL CHECK(length(payload) <= 8388608),
        sha256 TEXT NOT NULL CHECK(length(sha256)=64),
        PRIMARY KEY(session_id,seq),
        UNIQUE(session_id,turn_id),
        CHECK((seq=0 AND turn_id IS NULL) OR (seq>0 AND turn_id IS NOT NULL)),
        FOREIGN KEY(session_id,turn_id) REFERENCES turns(session_id,turn_id)
            DEFERRABLE INITIALLY DEFERRED
    )""",
)


# Legacy SQL is retained verbatim for schema 1/2 validation. Schema 3 broadens
# only the checkpoint format constraint; existing rows/receipts are unchanged.
_CHECKPOINT_SCHEMA_V3 = (
    _SCHEMA[-1]
    .replace("CREATE TABLE checkpoints", 'CREATE TABLE "checkpoints"')
    .replace("CHECK(format_version=1)", "CHECK(format_version IN (1,2))")
)
# SQLite appends rowid to this nonunique index, satisfying per-Session insertion
# order without sorting all Turn records into a temporary B-tree on every read.
_TURN_ORDER_INDEX = "CREATE INDEX session_turn_order ON turns(session_id)"
_SCHEMA_V3 = (*_SCHEMA[:-1], _CHECKPOINT_SCHEMA_V3, _TURN_ORDER_INDEX)

# Schema 4 moves every admitted fence out of `turns.diagnostics_json` into this
# append-only table, so appending fence N inserts one row instead of re-encoding
# the N-1 before it. The CHECK and UNIQUE constraints are the SQLite twins of the
# Strict Codec's field bounds and `_validate_record`'s reference set; the parts a
# CHECK cannot express (no control characters, timestamp monotonicity, ordinal
# contiguity, the cumulative bound) stay in the codec and the append path.
_TOOL_FENCE_SCHEMA = (
    """CREATE TABLE tool_fences (
        session_id TEXT NOT NULL,
        turn_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 1 AND 256),
        assistant_message_index INTEGER NOT NULL
            CHECK(assistant_message_index >= 0 AND assistant_message_index < 10000),
        call_id TEXT NOT NULL
            CHECK(length(CAST(call_id AS BLOB)) BETWEEN 1 AND 1024),
        tool_name TEXT NOT NULL
            CHECK(length(CAST(tool_name AS BLOB)) BETWEEN 1 AND 1024),
        observed_at_utc TEXT NOT NULL,
        PRIMARY KEY(session_id,turn_id,ordinal),
        UNIQUE(session_id,turn_id,assistant_message_index,call_id),
        FOREIGN KEY(session_id,turn_id) REFERENCES turns(session_id,turn_id)
    )""",
)
_SCHEMA_V4 = (*_SCHEMA_V3, *_TOOL_FENCE_SCHEMA)


_TASK_SCHEMA = (
    """CREATE TABLE tasks (
        session_id TEXT NOT NULL REFERENCES sessions(session_id),
        task_id TEXT NOT NULL,
        version INTEGER NOT NULL CHECK(version >= 0),
        phase TEXT NOT NULL CHECK(phase IN
            ('running','turn_committed','inspecting','result_recorded')),
        base_seq INTEGER NOT NULL CHECK(base_seq >= 0),
        checkpoint_seq INTEGER,
        record_json TEXT NOT NULL CHECK(length(CAST(record_json AS BLOB)) <= 4096),
        PRIMARY KEY(session_id,task_id),
        CHECK((phase='running' AND checkpoint_seq IS NULL)
            OR (phase!='running' AND checkpoint_seq IS NOT NULL AND checkpoint_seq=base_seq+1)),
        FOREIGN KEY(session_id,base_seq) REFERENCES checkpoints(session_id,seq),
        FOREIGN KEY(session_id,checkpoint_seq) REFERENCES checkpoints(session_id,seq)
    )""",
)


class _SessionRow(StrictDTO):
    revision: Revision
    state: SessionState
    active_turn_id: str | None
    binding: SessionBinding
    created_at_utc: str
    updated_at_utc: str


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


# The WAL-reset judgement lives in one neutral module so Project Memory can share
# it without importing this package. Re-exported here because this module has long
# been the documented import site for the session store's release gate.


def _unverified_database_error(version: tuple[int, int, int]) -> SessionStoreError:
    """Refuse an unverified runtime with the facts an operator needs."""
    current = ".".join(str(part) for part in version)
    return SessionStoreError(
        "sqlite_wal_unverified",
        f"current={current} requires={VERIFIED_SQLITE_VERSIONS}"
        f" declare={UNVERIFIED_SQLITE_DECLARATION}=1",
    )


# Session state transitions
# -----------------------------------------------------------------------
#
#  ┌─────────────────────┐
#  │        ready        │
#  └─────────────────────┘
#             │              begin_turn
#             v
#  ┌─────────────────────┐  require_recovery      ┌─────────────────────┐
#  │      executing      │───────────────────────>│  recovery_required  │
#  └─────────────────────┘                        └─────────────────────┘
#             │                                              │
#             │  commit_turn                                 │
#             │  (any outcome)                               │
#             │                                              │
#             v                                              │
#  ┌─────────────────────┐                                   │
#  │        ready        │                                   │
#  └─────────────────────┘                                   │
#             ^  reconcile                                   │
#             ┌──────────────────────────────────────────────┘
#
# Every arrow is one CAS on ``sessions.version``; no arrow writes a
# terminal Session. ``recovery_required`` is the only guarded state and
# it is left only through an explicit trusted-host resolution.
#
# ``active_turn_id`` is written by the same CAS as the state: None for
# ready, the Turn for executing. A recovery_required Session keeps
# whatever the faulted attempt left there until the host reconciles it.
# -----------------------------------------------------------------------


class SessionStore(TaskLedgerMixin):
    def __init__(
        self,
        directory: Path,
        *,
        mode: Literal["writer", "inspect"] = "writer",
        forbidden_roots: tuple[Path, ...],
        sensitive_values: tuple[str, ...] = (),
        allow_unverified: bool = False,
    ) -> None:
        if (
            mode not in ("writer", "inspect")
            or any(type(s) is not str for s in sensitive_values)
            or type(allow_unverified) is not bool
        ):
            raise SessionStoreError("invalid_options")
        # Runtime admission precedes every path, lock and database effect. A
        # runtime without the verified WAL-reset fix is never admitted silently;
        # declaring it admits an unverified store that no caller may report as
        # supported.
        version = sqlite3.sqlite_version_info
        admission = wal_admission(allow_unverified=allow_unverified, version=version)
        if admission == "refused":
            raise _unverified_database_error(version)
        self.wal_admission: Literal["verified", "unverified"] = admission
        self.mode = mode
        self.sensitive_values = sensitive_values
        self._connection: sqlite3.Connection | None = None
        self._paths = StorePaths(directory, forbidden_roots, writer=mode == "writer")
        self.directory = self._paths.directory
        self.path = self._paths.path
        self._fault = False
        self._fault_sessions: set[str] = set()
        self._mutation_session = "*"
        self._owned_turns: set[tuple[str, str]] = set()
        self._owned_tasks: set[tuple[str, str]] = set()
        self._inspected: dict[str, Revision] = {}
        self.maintenance_failed = False
        try:
            with self._paths.access():
                try:
                    self._connection = self._open()
                    if mode == "writer":
                        self._maintain()
                except BaseException:
                    if self._connection is not None:
                        self._connection.close()
                        self._connection = None
                    raise
                finally:
                    if mode == "inspect" and self._connection is not None:
                        self._connection.close()
                        self._connection = None
        except SessionStoreError:
            self.close()
            raise
        except (sqlite3.Error, OSError, ValueError, TypeError, AttributeError):
            self.close()
            raise SessionStoreError() from None

    def _open(self) -> sqlite3.Connection:
        self._paths.check()
        if self.mode == "inspect" and Path(str(self.path) + "-journal").exists():
            raise SessionStoreError("inspection_unavailable")
        # The caller holds the read/effect guard until this connection closes.
        # Immutable is safe only for this pinned, WAL-absent epoch. It ignores WAL.
        cold = self.mode == "inspect" and not Path(str(self.path) + "-wal").exists()
        options = "?mode=rw"
        if self.mode == "inspect":
            options = "?mode=ro&immutable=1" if cold else "?mode=ro&vfs=unix-none"
        conn = sqlite3.connect(
            self.path.as_uri() + options,
            uri=True,
            timeout=2,
            isolation_level=None,
        )
        try:
            if self.mode == "inspect":
                # The access guard excludes every Store SQLite effect. Heap WAL
                # indexing reads committed WAL without touching SHM or treating
                # an active WAL database as immutable.
                conn.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
                if not conn.getconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE):
                    raise SessionStoreError("sqlite_configuration_unavailable")
                if (
                    conn.execute("PRAGMA locking_mode=EXCLUSIVE").fetchone()[0]
                    != "exclusive"
                ):
                    raise SessionStoreError("sqlite_configuration_unavailable")
                conn.execute("PRAGMA query_only=ON")
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA trusted_schema=OFF")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=2000")
            if self._paths.new_database:
                if conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                    raise SessionStoreError("wal_unavailable")
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("BEGIN IMMEDIATE")
                for statement in (*_SCHEMA_V4, *_TASK_SCHEMA):
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                conn.commit()
            self._schema(conn)
            if not cold and conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                raise SessionStoreError("wal_unavailable")
            if self.mode == "writer":
                conn.execute("PRAGMA synchronous=FULL")
                page_size = conn.execute("PRAGMA page_size").fetchone()[0]
                if (
                    conn.execute(
                        f"PRAGMA max_page_count={MAX_DATABASE_BYTES // page_size}"
                    ).fetchone()[0]
                    * page_size
                    > MAX_DATABASE_BYTES
                ):
                    raise SessionStoreError("database_limit")
                conn.execute("PRAGMA wal_autocheckpoint=0")
            required = {"foreign_keys": 1, "trusted_schema": 0, "busy_timeout": 2000}
            required["synchronous" if self.mode == "writer" else "query_only"] = (
                2 if self.mode == "writer" else 1
            )
            for pragma, expected in required.items():
                setting = conn.execute(f"PRAGMA {pragma}").fetchone()
                if setting is None or setting[0] != expected:
                    raise SessionStoreError("sqlite_configuration_unavailable")
            self._paths.check()
        except BaseException:
            conn.close()
            raise
        return conn

    def _conn(self) -> sqlite3.Connection:
        if self._connection is None:
            raise SessionStoreError("closed_store")
        self._paths.check()
        return self._connection

    def _schema(self, conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        # Every schema this build can read: the legacy versions it can migrate
        # from, and the one it writes.
        if version not in (1, 2, 3, SCHEMA_VERSION):
            raise SessionStoreError("schema_mismatch")
        observed = {
            " ".join(row[0].split())
            for row in conn.execute(
                "SELECT sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
            )
        }
        expected = (
            _SCHEMA
            if version == 1
            else (*_SCHEMA, *_TASK_SCHEMA)
            if version == 2
            else (*_SCHEMA_V3, *_TASK_SCHEMA)
            if version == 3
            else (*_SCHEMA_V4, *_TASK_SCHEMA)
        )
        if observed != {" ".join(s.split()) for s in expected}:
            raise SessionStoreError("schema_mismatch")
        if (
            conn.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok"
            or conn.execute("PRAGMA foreign_key_check").fetchone() is not None
        ):
            raise SessionStoreError("database_integrity")
        # Bound TEXT/BLOB retrieval too, rather than allocate an arbitrary hostile
        # row and only then check its decoded size.
        bounds: tuple[tuple[str, str, int, str], ...] = (
            ("sessions", "binding_json", MAX_RECORD_BYTES, "text"),
            ("turns", "diagnostics_json", MAX_RECORD_BYTES, "text"),
            ("checkpoints", "payload", MAX_CHECKPOINT_BYTES, "blob"),
        )
        if version >= 2:
            bounds += (("tasks", "record_json", MAX_TASK_BYTES, "text"),)
        if version >= 4:
            bounds += (("tool_fences", "call_id", 1024, "text"),)
            bounds += (("tool_fences", "tool_name", 1024, "text"),)
        for table, column, limit, sql_type in bounds:
            if (
                conn.execute(
                    f"SELECT 1 FROM {table} WHERE typeof({column}) != ? OR length(CAST({column} AS BLOB)) > ? LIMIT 1",
                    (sql_type, limit),
                ).fetchone()
                is not None
            ):
                raise SessionStoreError("record_limit")

    def _encode(
        self,
        value: StrictDTO,
        *,
        limit: int = MAX_RECORD_BYTES,
        kind: type[StrictDTO] | None = None,
    ) -> bytes:
        try:
            raw = encode_record(
                value, limit=limit, sensitive_values=self.sensitive_values
            )
            if kind is not None:
                decode_record(
                    kind, raw, limit=limit, sensitive_values=self.sensitive_values
                )
            return raw
        except SessionStoreError:
            self._latch()
            raise

    @staticmethod
    def _without_fences(diagnostics: AttemptDiagnostics) -> AttemptDiagnostics:
        """A diagnostics record with the fence tuple emptied.

        `tool_fences` is the only fence of record at schema V4. Every write to
        `turns.diagnostics_json` goes through here so the JSON column can never
        hold a second copy that drifts from the table.
        """
        if not diagnostics.fences:
            return diagnostics
        return diagnostics.model_copy(update={"fences": ()})

    def _diagnostics_for_json(
        self, conn: sqlite3.Connection, diagnostics: AttemptDiagnostics
    ) -> AttemptDiagnostics:
        """The diagnostics this database stores in JSON.

        Only a schema that has `tool_fences` may drop the fences, because there
        the table is their source of truth. A legacy schema keeps them inline:
        emptying the tuple there would delete the Turn's only fence evidence.
        """
        if self._has_fence_table(conn):
            return self._without_fences(diagnostics)
        return diagnostics

    def _begin(self, expected: Revision | TaskRevision, kind: type[StrictDTO]) -> None:
        """Attribute a later fault to this caller's session, then validate.

        Every public mutator opens with these two steps in this order:
        ``_latch`` reads ``_mutation_session``, so it must be set before the
        validation that could fail, and the validation must precede any
        transaction or external effect.
        """
        self._mutation_session = expected.session_id
        self._encode(expected, kind=kind)

    def _latch(self) -> None:
        self._fault = True
        self._fault_sessions.add(
            self._mutation_session if type(self._mutation_session) is str else "*"
        )
        # Storage failure revokes write eligibility, not runtime liveness. Only
        # settled commit or explicit host release after cleanup drops ownership.
        self._inspected.clear()

    def _maintain(self) -> None:
        """Idle, bounded WAL housekeeping; its error never revokes a receipt."""
        try:
            conn = self._conn()
            if self.mode == "writer":
                result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                self.maintenance_failed = result[0] != 0
                self._paths.check()
        except (sqlite3.Error, OSError, SessionStoreError):
            self.maintenance_failed = True

    def _wal_size(self) -> int:
        try:
            return Path(str(self.path) + "-wal").stat().st_size
        except FileNotFoundError:
            return 0

    @contextmanager
    def _write(
        self,
        *,
        recovery: bool = False,
        idle_after: bool = False,
        migration: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        conn: sqlite3.Connection | None = None
        committing = False
        try:
            with self._paths.access():
                try:
                    if self.mode != "writer":
                        raise SessionStoreError("read_only_store")
                    if self._fault and not recovery:
                        raise SessionStoreError("recovery_required")
                    conn = self._conn()
                    # A failed migration cleanup must never admit recovery writes
                    # on a connection whose FK enforcement could not be restored.
                    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                        raise SessionStoreError("sqlite_configuration_unavailable")
                    if self._wal_size() >= MAX_WAL_BYTES:
                        if not self._owned_turns or recovery:
                            self._maintain()
                        if self._wal_size() >= MAX_WAL_BYTES:
                            raise SessionStoreError("wal_admission_limit")
                    if migration:
                        conn.execute("PRAGMA foreign_keys=OFF")
                    conn.execute("BEGIN IMMEDIATE")
                    self._schema(conn)
                    yield conn
                    self._paths.check()
                    committing = True
                    self._commit(conn)
                except BaseException:
                    if conn is not None:
                        with suppress(sqlite3.Error):
                            conn.rollback()
                    raise
                finally:
                    if migration and conn is not None:
                        conn.execute("PRAGMA foreign_keys=ON")
                        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                            raise SessionStoreError("sqlite_configuration_unavailable")
                if idle_after:
                    self._maintain()
        except BaseException as exc:
            self._latch()
            if isinstance(exc, SessionStoreError):
                raise
            if isinstance(exc, Exception):
                raise SessionStoreError(
                    "commit_uncertain" if committing else "storage_failed"
                ) from None
            raise

    def _commit(self, conn: sqlite3.Connection) -> None:
        conn.commit()

    def _row(self, conn: sqlite3.Connection, expected: Revision) -> _SessionRow:
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id=?", (expected.session_id,)
        ).fetchone()
        if row is None:
            raise SessionStoreError("session_missing")
        binding = decode_record(
            SessionBinding,
            row["binding_json"].encode(),
            sensitive_values=self.sensitive_values,
        )
        result = _SessionRow(
            revision=Revision(
                session_id=row["session_id"],
                version=row["version"],
                head_seq=row["head_seq"],
            ),
            state=row["state"],
            active_turn_id=row["active_turn_id"],
            binding=binding,
            created_at_utc=row["created_at_utc"],
            updated_at_utc=row["updated_at_utc"],
        )
        utc_text(result.created_at_utc)
        utc_text(result.updated_at_utc)
        # Host wall-clock observations may move backwards. Revision/CAS, not
        # created/updated timestamp ordering, establishes the mutation order.
        if binding.session_id != expected.session_id:
            raise SessionStoreError("invalid_relationship")
        if result.active_turn_id is not None:
            uuid4_text(result.active_turn_id)
        if result.revision != expected:
            raise VersionConflict()
        return result

    def _cas(
        self,
        conn: sqlite3.Connection,
        expected: Revision,
        *,
        state: SessionState,
        active: str | None,
        binding: SessionBinding,
        head: int | None = None,
    ) -> Revision:
        if expected.version >= MAX_INTEGER:
            raise SessionStoreError("revision_limit")
        revision = Revision(
            session_id=expected.session_id,
            version=expected.version + 1,
            head_seq=expected.head_seq if head is None else head,
        )
        result = conn.execute(
            "UPDATE sessions SET version=?,head_seq=?,state=?,active_turn_id=?,binding_json=?,updated_at_utc=? WHERE session_id=? AND version=? AND head_seq=?",
            (
                revision.version,
                revision.head_seq,
                state,
                active,
                self._encode(binding).decode(),
                _now(),
                expected.session_id,
                expected.version,
                expected.head_seq,
            ),
        )
        if result.rowcount != 1:
            raise VersionConflict()
        return revision

    def _records(
        self, conn: sqlite3.Connection, session_id: str
    ) -> tuple[TurnRecord, ...]:
        result: list[TurnRecord] = []
        for row in conn.execute(
            "SELECT * FROM turns WHERE session_id=? ORDER BY rowid", (session_id,)
        ):
            record = self._turn_record(conn, row)
            self._validate_record(record)
            result.append(record)
        return tuple(result)

    def _turn_record(
        self, conn: sqlite3.Connection, row: sqlite3.Row, *, with_fences: bool = True
    ) -> TurnRecord:
        """One TurnRecord, with its fences reassembled from `tool_fences`.

        `with_fences=False` skips the child rows. The append path only needs the
        Turn's identity and state, and decoding every prior fence there is the
        quadratic term this schema removes.
        """
        diagnostics = decode_record(
            AttemptDiagnostics,
            row["diagnostics_json"].encode(),
            sensitive_values=self.sensitive_values,
        )
        if with_fences and self._has_fence_table(conn):
            # At V4 the table is the only fence source. On a legacy schema there
            # is no table and the JSON copy is the only one, so it stands.
            fences = self._fences_for(conn, row["session_id"], row["turn_id"])
            diagnostics = diagnostics.model_copy(update={"fences": fences})
        return TurnRecord(
            intent=TurnIntent(
                session_id=row["session_id"],
                turn_id=row["turn_id"],
                base_seq=row["base_seq"],
                message_start=diagnostics.message_start,
                input_digest=row["input_digest"],
                started_at_utc=row["started_at_utc"],
                task_id=row["task_id"],
                binding=diagnostics.binding,
            ),
            state=row["state"],
            ended_at_utc=row["ended_at_utc"],
            settled_seq=row["settled_seq"],
            diagnostics=diagnostics,
        )

    @staticmethod
    def _has_fence_table(conn: sqlite3.Connection) -> bool:
        """Whether this database has the V4 fence table yet."""
        return (
            conn.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='tool_fences'"
            ).fetchone()
            is not None
        )

    def _fences_for(
        self, conn: sqlite3.Connection, session_id: str, turn_id: str
    ) -> tuple[ToolFence, ...]:
        # A legacy schema has no fence table; its fences are still in the JSON
        # column and `decode_record` above has already loaded them.
        if not self._has_fence_table(conn):
            return ()
        rows = conn.execute(
            "SELECT * FROM tool_fences WHERE session_id=? AND turn_id=? ORDER BY ordinal",
            (session_id, turn_id),
        ).fetchall()
        fences: list[ToolFence] = []
        for row in rows:
            # Decode through the record codec so the sensitive-value and
            # credential screens that every stored record passes also run here.
            # Constructing the DTO directly would let a row that was written by
            # an older build (or a bypass) come back unscreened.
            raw = canonical(
                {
                    "session_id": row["session_id"],
                    "turn_id": row["turn_id"],
                    "ordinal": row["ordinal"],
                    "assistant_message_index": row["assistant_message_index"],
                    "call_id": row["call_id"],
                    "tool_name": row["tool_name"],
                    "observed_at_utc": row["observed_at_utc"],
                }
            )
            fences.append(
                decode_record(ToolFence, raw, sensitive_values=self.sensitive_values)
            )
        return tuple(fences)

    def _validate_record(self, record: TurnRecord) -> None:
        intent, diagnostic = record.intent, record.diagnostics
        references: set[tuple[int, str]] = set()
        index = intent.message_start
        timestamp = intent.started_at_utc
        for ordinal, fence in enumerate(diagnostic.fences, 1):
            if (
                fence.session_id != intent.session_id
                or fence.turn_id != intent.turn_id
                or fence.ordinal != ordinal
                or fence.assistant_message_index < index
                or instant(fence.observed_at_utc) < instant(timestamp)
                or (fence.assistant_message_index, fence.call_id) in references
            ):
                raise SessionStoreError("invalid_fence")
            references.add((fence.assistant_message_index, fence.call_id))
            index, timestamp = fence.assistant_message_index, fence.observed_at_utc
        terminal = record.state in ("completed", "interrupted")
        if terminal != (
            record.settled_seq is not None and diagnostic.commit_version is not None
        ):
            raise SessionStoreError("invalid_relationship")
        if terminal and diagnostic.reason is not None:
            raise SessionStoreError("invalid_relationship")
        if record.state == "recovery_required" and diagnostic.reason is None:
            raise SessionStoreError("invalid_relationship")
        if (record.state == "abandoned" and diagnostic.commit_version is not None) or (
            record.state == "executing" and diagnostic.reason is not None
        ):
            raise SessionStoreError("invalid_relationship")
        if record.state in ("executing", "recovery_required"):
            if (
                record.ended_at_utc is not None
                or diagnostic.resolution is not None
                or record.settled_seq is not None
                or diagnostic.commit_version is not None
            ):
                raise SessionStoreError("invalid_relationship")
        else:
            if record.ended_at_utc is None or instant(
                utc_text(record.ended_at_utc)
            ) < instant(timestamp):
                raise SessionStoreError("invalid_relationship")
            if (record.state == "abandoned") != (diagnostic.resolution is not None):
                raise SessionStoreError("invalid_relationship")
        if diagnostic.resolution is not None:
            resolution = diagnostic.resolution
            if (
                resolution.session_id != intent.session_id
                or resolution.turn_id != intent.turn_id
                or resolution.resolved_at_utc != record.ended_at_utc
                or not same_workspace(intent.binding, resolution.binding)
            ):
                raise SessionStoreError("invalid_relationship")

    def _admit_diagnostics(
        self, intent: TurnIntent, diagnostics: AttemptDiagnostics
    ) -> None:
        """Leave space to settle or abandon every admitted intent/fence.

        This is size accounting only, never a persisted or approved resolution.
        Reserve bounded actor/note, timestamps, counters and HEAD/branch drift
        with the current provider reference. A larger future reference must still
        fit the record, or be acknowledged separately after abandonment.
        """
        binding = intent.binding
        if binding.workspace.git is not None:
            binding = binding.model_copy(
                update={
                    "workspace": binding.workspace.model_copy(
                        update={
                            "git": binding.workspace.git.model_copy(
                                update={"head": "f" * 64, "branch": "\U0010fffd" * 256}
                            )
                        }
                    )
                }
            )
        timestamp = "9999-12-31T23:59:59.999999Z"
        binding = binding.model_copy(update={"observed_at_utc": timestamp})
        resolution = HostResolution(
            session_id=intent.session_id,
            turn_id=intent.turn_id,
            actor="\U0010fffd" * 256,
            note="\U0010fffd" * 1024,
            resolved_at_utc=timestamp,
            binding=binding,
        )
        reserved = diagnostics.model_copy(
            update={
                "reason": "finalization_failed",
                "resolution": resolution,
                "commit_version": MAX_INTEGER,
            }
        )
        if len(canonical(reserved.model_dump(mode="json"))) > MAX_RECORD_BYTES:
            raise SessionStoreError("record_limit")

    def _validate_fenced_history(
        self, record: TurnRecord, checkpoint: CheckpointPayload | CheckpointDelta
    ) -> None:
        offset = (
            checkpoint.message_range.start
            if isinstance(checkpoint, CheckpointDelta)
            else 0
        )
        previous = (-1, -1)
        for fence in record.diagnostics.fences:
            index = fence.assistant_message_index - offset
            if not 0 <= index < len(checkpoint.messages):
                raise SessionStoreError("fenced_history_missing")
            message = checkpoint.messages[index]
            position = next(
                (
                    i
                    for i, c in enumerate(message.tool_calls)
                    if c.id == fence.call_id and c.name == fence.tool_name
                ),
                None,
            )
            if (
                message.role != "assistant"
                or position is None
                or (fence.assistant_message_index, position) <= previous
            ):
                raise SessionStoreError("invalid_fence")
            if instant(fence.observed_at_utc) > instant(checkpoint.settled_at_utc):
                raise SessionStoreError("invalid_fence")
            previous = (fence.assistant_message_index, position)

    def _checkpoint_row(self, row: sqlite3.Row) -> CheckpointPayload | CheckpointDelta:
        """Decode one checkpoint row, enforcing its byte bound."""
        if (
            not isinstance(row["payload"], bytes)
            or len(row["payload"]) > MAX_CHECKPOINT_BYTES
        ):
            raise SessionStoreError("record_limit")
        return decode_storage_checkpoint(
            row["payload"],
            row["sha256"],
            format_version=row["format_version"],
            sensitive_values=self.sensitive_values,
        )

    def _validate_unresolved_turn(
        self, session: _SessionRow, unresolved: Sequence[TurnRecord]
    ) -> None:
        """At most one turn may be unresolved, and it must match the session."""
        if not unresolved:
            if session.state == "executing":
                raise SessionStoreError("executing_session_without_unresolved_turn")
            return
        if len(unresolved) > 1:
            raise SessionStoreError("multiple_unresolved_turns")
        active = unresolved[0]
        if active.intent.turn_id != session.active_turn_id:
            raise SessionStoreError("unresolved_turn_not_active")
        if session.state == "ready":
            raise SessionStoreError("ready_session_with_unresolved_turn")
        if session.state != active.state:
            raise SessionStoreError("unresolved_turn_state_mismatch")

    def _validate_record_bases(
        self,
        session: _SessionRow,
        revision: Revision,
        records: Sequence[TurnRecord],
        committed: set[str],
        bases: dict[int, tuple[int, str]],
    ) -> None:
        """Committed state, binding and base chain must agree with each record."""
        expected_base = 0
        for record in records:
            settled = record.state in ("completed", "interrupted")
            if settled != (record.intent.turn_id in committed):
                raise SessionStoreError("record_commit_state_mismatch")
            if record.intent.base_seq > revision.head_seq:
                raise SessionStoreError("record_base_ahead_of_head")
            if not same_workspace(session.binding, record.intent.binding):
                raise SessionStoreError("record_binding_mismatch")
            base = bases.get(record.intent.base_seq)
            if record.intent.base_seq != expected_base:
                raise SessionStoreError("record_base_chain_gap")
            if base is None:
                raise SessionStoreError("record_base_checkpoint_missing")
            if record.intent.message_start != base[0]:
                raise SessionStoreError("record_message_start_mismatch")
            if instant(record.intent.started_at_utc) < instant(base[1]):
                raise SessionStoreError("record_started_before_base")
            if settled:
                expected_base += 1

    def _report(self, conn: sqlite3.Connection, session_id: str) -> RecoveryReport:
        raw = conn.execute(
            "SELECT version,head_seq FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if raw is None:
            raise SessionStoreError("session_missing")
        revision = Revision(session_id=session_id, version=raw[0], head_seq=raw[1])
        session = self._row(conn, revision)
        records = self._records(conn, session_id)
        by_turn = {r.intent.turn_id: r for r in records}
        previous: CheckpointPayload | CheckpointDelta | None = None
        receipt: CheckpointReceipt | None = None
        committed: set[str] = set()
        bases: dict[int, tuple[int, str]] = {}
        history: list[StoredMessage] = []
        history_bytes = 0
        for row in conn.execute(
            "SELECT * FROM checkpoints WHERE session_id=? ORDER BY seq", (session_id,)
        ):
            checkpoint = self._checkpoint_row(row)
            # Validate row identity before following the checkpoint chain.
            if checkpoint.session_id != session_id:
                raise SessionStoreError("checkpoint_session_mismatch")
            if checkpoint.seq != row["seq"]:
                raise SessionStoreError("checkpoint_seq_mismatch")
            if checkpoint.turn_id != row["turn_id"]:
                raise SessionStoreError("checkpoint_turn_mismatch")
            if row["format_version"] != checkpoint.format_version:
                raise SessionStoreError("checkpoint_format_version_mismatch")
            if row["created_at_utc"] != checkpoint.settled_at_utc:
                raise SessionStoreError("checkpoint_settled_time_mismatch")
            expected_seq = 0 if previous is None else previous.seq + 1
            if checkpoint.seq != expected_seq:
                raise SessionStoreError("checkpoint_sequence_gap")
            if not same_workspace(session.binding, checkpoint.binding):
                raise SessionStoreError("checkpoint_binding_mismatch")
            if isinstance(checkpoint, CheckpointDelta):
                if checkpoint.predecessor != receipt:
                    raise SessionStoreError("checkpoint_predecessor_mismatch")
                if checkpoint.message_range.start != len(history):
                    raise SessionStoreError("invalid_delta_range")
                new_messages = checkpoint.messages
            else:
                # Once incremental storage begins, a full snapshot is a downgrade.
                if isinstance(previous, CheckpointDelta):
                    raise SessionStoreError("checkpoint_format_downgrade")
                history = []
                history_bytes = 0
                new_messages = checkpoint.messages
            history_bytes += sum(
                len(canonical(m.model_dump(mode="json"))) + 1 for m in new_messages
            )
            if (
                history_bytes > MAX_CHECKPOINT_BYTES
                or len(history) + len(new_messages) > MAX_MESSAGES
            ):
                raise SessionStoreError("record_limit")
            history.extend(new_messages)
            # A receipt must agree with its settled Turn and prior receipt.
            commit_version = 0
            if previous is not None:
                record = by_turn.get(checkpoint.turn_id or "")
                if record is None:
                    raise SessionStoreError("checkpoint_without_turn_record")
                self._settled_relationship(record, checkpoint, previous)
                commit_version = record.diagnostics.commit_version or 0
                if receipt is None:
                    raise SessionStoreError("checkpoint_receipt_missing")
                if commit_version <= receipt.revision.version:
                    raise SessionStoreError("checkpoint_commit_version_not_advancing")
                if commit_version > revision.version:
                    raise SessionStoreError("checkpoint_commit_version_ahead_of_head")
                committed.add(record.intent.turn_id)
            receipt = CheckpointReceipt(
                revision=Revision(
                    session_id=session_id,
                    version=commit_version,
                    head_seq=checkpoint.seq,
                ),
                turn_id=checkpoint.turn_id,
                sha256=row["sha256"],
                outcome=checkpoint.outcome,
            )
            previous = checkpoint
            bases[checkpoint.seq] = (
                checkpoint.message_range.end,
                checkpoint.settled_at_utc,
            )
        if previous is None or receipt is None:
            raise SessionStoreError("committed_head_missing")
        if previous.seq != revision.head_seq:
            raise SessionStoreError("committed_head_seq_mismatch")
        unresolved = [
            r for r in records if r.state in ("executing", "recovery_required")
        ]
        self._validate_unresolved_turn(session, unresolved)
        self._validate_record_bases(session, revision, records, committed, bases)
        if unresolved:
            active = unresolved[0]
            if active.intent.base_seq != revision.head_seq:
                raise SessionStoreError("unresolved_turn_base_not_head")
            if active.intent.message_start != previous.message_range.end:
                raise SessionStoreError("unresolved_turn_message_start_mismatch")
            if active.intent.binding != session.binding:
                raise SessionStoreError("unresolved_turn_binding_mismatch")
        if isinstance(previous, CheckpointDelta):
            materialized = CheckpointPayload(
                **previous.model_dump(
                    exclude={"format_version", "messages", "predecessor"}
                ),
                messages=tuple(history),
            )
            # Bound the exact full host envelope as well as the incremental bytes.
            # This single final pass is linear; no repeated materialized prefixes.
            encode_record(
                materialized,
                limit=MAX_CHECKPOINT_BYTES,
                sensitive_values=self.sensitive_values,
            )
        else:
            materialized = previous
        status: Literal["clean", "executing", "effects_unknown", "recovery_required"]
        if session.state == "recovery_required" or self._fault:
            status = "recovery_required"
        elif unresolved:
            status = (
                "effects_unknown" if unresolved[0].diagnostics.fences else "executing"
            )
        else:
            status = "clean"
        return RecoveryReport(
            revision=revision,
            state=session.state,
            status=status,
            binding=session.binding,
            checkpoint=materialized,
            receipt=receipt,
            attempts=records,
            local_fault=self._fault,
        )

    def _settled_relationship(
        self,
        record: TurnRecord,
        checkpoint: CheckpointPayload | CheckpointDelta,
        previous: CheckpointPayload | CheckpointDelta,
    ) -> None:
        intent = record.intent
        if (
            checkpoint.seq != intent.base_seq + 1
            or checkpoint.turn_id != intent.turn_id
            or checkpoint.message_range.start != intent.message_start
            or intent.message_start != previous.message_range.end
            or (
                isinstance(checkpoint, CheckpointPayload)
                and checkpoint.messages[: previous.message_range.end]
                != previous.messages
            )
            or checkpoint.started_at_utc != intent.started_at_utc
            or not same_workspace(intent.binding, checkpoint.binding)
            or checkpoint.binding.provider != intent.binding.provider
            or instant(checkpoint.binding.observed_at_utc)
            < instant(intent.binding.observed_at_utc)
            or record.state
            != ("completed" if checkpoint.outcome == "completed" else "interrupted")
            or record.settled_seq != checkpoint.seq
            or record.ended_at_utc != checkpoint.settled_at_utc
        ):
            raise SessionStoreError("invalid_settled_history")
        tail = (
            checkpoint.messages
            if isinstance(checkpoint, CheckpointDelta)
            else checkpoint.messages[intent.message_start :]
        )
        if tail and (
            # Intent binds the exact host input, without storing it before settlement.
            hashlib.sha256((tail[0].content or "").encode("utf-8")).hexdigest()
            != intent.input_digest
        ):
            raise SessionStoreError("input_digest_mismatch")
        self._validate_fenced_history(record, checkpoint)

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        try:
            with self._paths.access():
                try:
                    if self.mode == "inspect":
                        self._connection = self._open()
                    conn = self._conn()
                    conn.execute("BEGIN")
                    try:
                        self._schema(conn)
                        yield conn
                        self._paths.check()
                    finally:
                        conn.rollback()
                finally:
                    if self.mode == "inspect" and self._connection is not None:
                        self._connection.close()
                        self._connection = None
        except SessionStoreError:
            raise
        except (
            sqlite3.Error,
            OSError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
        ):
            raise SessionStoreError("invalid_or_unavailable_store") from None

    def _snapshot(self, session_id: str) -> RecoveryReport:
        with self._read() as conn:
            uuid4_text(session_id)
            return self._report(conn, session_id)

    def inspect(self, session_id: str) -> RecoveryReport:
        report = self._snapshot(session_id)
        self._inspected[session_id] = report.revision
        return report

    @property
    def schema_version(self) -> int:
        with self._read() as conn:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])

    def _logical_reports(
        self, conn: sqlite3.Connection
    ) -> dict[str, tuple[Revision, tuple[TurnRecord, ...], CheckpointReceipt]]:
        """The Session state that must not change across a migration.

        Fences move from `diagnostics_json` to `tool_fences`; their *content* is
        what has to survive, so the reports are compared as the host sees them,
        not as rows.
        """
        reports: dict[
            str, tuple[Revision, tuple[TurnRecord, ...], CheckpointReceipt]
        ] = {}
        for row in conn.execute("SELECT session_id FROM sessions ORDER BY session_id"):
            report = self._report(conn, row[0])
            reports[row[0]] = (report.revision, report.attempts, report.receipt)
        return reports

    def _validate_sessions(self, conn: sqlite3.Connection) -> None:
        for row in conn.execute("SELECT session_id FROM sessions"):
            self._report(conn, row[0])
        if conn.execute("PRAGMA user_version").fetchone()[0] >= 2:
            for row in conn.execute("SELECT session_id,task_id FROM tasks"):
                self._task(conn, row[0], row[1])

    def _backup_schema(self, conn: sqlite3.Connection) -> Path:
        path = self._paths.create_backup(
            conn.execute("PRAGMA user_version").fetchone()[0]
        )
        destination = sqlite3.connect(
            path.as_uri() + "?mode=rw", uri=True, isolation_level=None
        )
        try:
            destination.row_factory = sqlite3.Row
            destination.execute("PRAGMA trusted_schema=OFF")
            destination.execute("PRAGMA foreign_keys=ON")
            destination.execute("PRAGMA synchronous=FULL")
            # Backup reads the SQLite snapshot, including committed WAL pages.
            # It must precede BEGIN IMMEDIATE on the source connection.
            conn.backup(destination, pages=256, sleep=0)
            if (
                destination.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
                != "delete"
            ):
                raise SessionStoreError("backup_failed")
            self._schema(destination)
            self._validate_sessions(destination)
        finally:
            destination.close()
        self._paths.sync_backup(path)
        return path

    def _migrate_v1(self, conn: sqlite3.Connection) -> None:
        for statement in _TASK_SCHEMA:
            conn.execute(statement)
        conn.execute("PRAGMA user_version=2")

    def _migrate_v2(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            _CHECKPOINT_SCHEMA_V3.replace('"checkpoints"', "checkpoints_upgrade", 1)
        )
        conn.execute("INSERT INTO checkpoints_upgrade SELECT * FROM checkpoints")
        conn.execute("DROP TABLE checkpoints")
        conn.execute("ALTER TABLE checkpoints_upgrade RENAME TO checkpoints")
        conn.execute(_TURN_ORDER_INDEX)
        conn.execute("PRAGMA user_version=3")

    def _migrate_v3(self, conn: sqlite3.Connection) -> None:
        """Move every fence from `turns.diagnostics_json` into `tool_fences`.

        Runs inside the migration transaction after a verified backup. Fences are
        copied in ordinal order with their identity intact; each `diagnostics_json`
        is then rewritten without them. Anything that does not decode, does not
        order contiguously, or repeats a reference aborts the whole step, leaving
        the V3 database untouched.
        """
        for statement in _TOOL_FENCE_SCHEMA:
            conn.execute(statement)
        for row in conn.execute(
            "SELECT session_id, turn_id, diagnostics_json FROM turns ORDER BY rowid"
        ).fetchall():
            diagnostics = decode_record(
                AttemptDiagnostics,
                row["diagnostics_json"].encode(),
                sensitive_values=self.sensitive_values,
            )
            for expected_ordinal, fence_value in enumerate(diagnostics.fences, 1):
                if (
                    fence_value.ordinal != expected_ordinal
                    or fence_value.session_id != row["session_id"]
                    or fence_value.turn_id != row["turn_id"]
                ):
                    raise SessionStoreError("invalid_fence")
                self._encode(fence_value, kind=ToolFence)
                conn.execute(
                    "INSERT INTO tool_fences VALUES (?,?,?,?,?,?,?)",
                    (
                        fence_value.session_id,
                        fence_value.turn_id,
                        fence_value.ordinal,
                        fence_value.assistant_message_index,
                        fence_value.call_id,
                        fence_value.tool_name,
                        fence_value.observed_at_utc,
                    ),
                )
            stripped = self._without_fences(diagnostics)
            raw = self._encode(stripped)
            conn.execute(
                "UPDATE turns SET diagnostics_json=? WHERE session_id=? AND turn_id=?",
                (raw.decode(), row["session_id"], row["turn_id"]),
            )
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def upgrade_schema(self) -> Path:
        """Explicit trusted-host upgrade to the current schema; keep a backup.

        The lifetime writer lock is required. No locally owned Turn may be live.
        Existing Session operations remain supported on v1 without this call.
        """
        self._mutation_session = "*"
        try:
            with self._paths.access():
                if self.mode != "writer":
                    raise SessionStoreError("read_only_store")
                if self._owned_turns:
                    raise SessionStoreError("turn_still_owned")
                if self._owned_tasks:
                    raise SessionStoreError("task_still_owned")
                if self._fault:
                    raise SessionStoreError("recovery_required")
                conn = self._conn()
                self._schema(conn)
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version == SCHEMA_VERSION:
                    raise SessionStoreError("schema_upgrade_not_required")
                self._validate_sessions(conn)
                # D4 step 1: fix the logical state before anything moves. The
                # post-migration reports are compared with these, so a copy bug
                # cannot commit a schema bump that silently lost fence evidence.
                before = self._logical_reports(conn)
                backup = self._backup_schema(conn)
            with self._write(idle_after=True, migration=True) as conn:
                if conn.execute("PRAGMA user_version").fetchone()[0] != version:
                    raise SessionStoreError("schema_mismatch")
                if version == 1:
                    self._migrate_v1(conn)
                if version <= 2:
                    self._migrate_v2(conn)
                self._migrate_v3(conn)
                self._schema(conn)
                self._validate_sessions(conn)
                # D4 step 5: the same Sessions must read back identically.
                if self._logical_reports(conn) != before:
                    raise SessionStoreError("schema_upgrade_failed")
            return backup
        except SessionStoreError:
            raise
        except Exception:
            self._latch()
            raise SessionStoreError("schema_upgrade_failed") from None

    def _tasks_supported(self, conn: sqlite3.Connection) -> None:
        if conn.execute("PRAGMA user_version").fetchone()[0] < 2:
            raise SessionStoreError("schema_upgrade_required")

    def _fences_supported(self, conn: sqlite3.Connection) -> None:
        """Fencing needs the fence table introduced at schema 4."""
        if conn.execute("PRAGMA user_version").fetchone()[0] < 4:
            raise SessionStoreError("schema_upgrade_required")

    @contextmanager
    def _prepare(
        self, expected: Revision, *, recovery: bool = False
    ) -> Iterator[RecoveryReport]:
        """Read/prevalidate combined payloads before opening a mutation transaction."""
        try:
            if self.mode != "writer":
                raise SessionStoreError("read_only_store")
            if self._fault and not recovery:
                raise SessionStoreError("recovery_required")
            report = self._snapshot(expected.session_id)
            if report.revision != expected:
                raise VersionConflict()
            yield report
        except SessionStoreError as error:
            if error.code != "turn_still_owned":
                self._latch()
            raise

    def _current(
        self, conn: sqlite3.Connection, expected: Revision, prepared: RecoveryReport
    ) -> RecoveryReport:
        """Re-read the row inside the write transaction and confirm nothing moved.

        `_prepare` already read `prepared` and proved its revision equals
        `expected`; `_row` re-reads the stored row and raises `VersionConflict`
        unless the revision still matches. `_cas` is the only writer of
        `version`/`head_seq` and advances both on every mutation, so an unchanged
        revision proves the body `prepared` describes is still on disk. Rebuilding
        the whole report here would re-decode every attempt record on each
        mutation for the same answer.
        """
        self._row(conn, expected)
        return prepared

    def create(self, binding: SessionBinding) -> CheckpointReceipt:
        self._mutation_session = binding.session_id
        raw_binding = self._encode(binding, kind=SessionBinding)
        binding = decode_record(SessionBinding, raw_binding)
        checkpoint = CheckpointPayload(
            session_id=binding.session_id,
            turn_id=None,
            seq=0,
            binding=binding,
            messages=(),
            message_range=MessageRange(start=0, end=0),
            outcome=None,
            started_at_utc=None,
            settled_at_utc=binding.observed_at_utc,
        )
        format_version = 2 if self.schema_version >= 3 else 1
        payload, digest = encode_storage_checkpoint(
            checkpoint,
            None,
            format_version=format_version,
            sensitive_values=self.sensitive_values,
        )
        with self._write(idle_after=True) as conn:
            if format_version != (
                2 if conn.execute("PRAGMA user_version").fetchone()[0] >= 3 else 1
            ):
                raise VersionConflict()
            conn.execute(
                "INSERT INTO sessions VALUES (?,?,?,'ready',NULL,?,?,?)",
                (
                    binding.session_id,
                    0,
                    0,
                    raw_binding.decode(),
                    binding.observed_at_utc,
                    binding.observed_at_utc,
                ),
            )
            conn.execute(
                "INSERT INTO checkpoints VALUES (?,0,NULL,?,?,?,?)",
                (
                    binding.session_id,
                    format_version,
                    checkpoint.settled_at_utc,
                    payload,
                    digest,
                ),
            )
        try:
            self._paths.sync_creation()
        except (OSError, SessionStoreError):
            self._latch()
            raise SessionStoreError("create_acknowledgement_uncertain") from None
        return CheckpointReceipt(
            revision=Revision(session_id=binding.session_id, version=0, head_seq=0),
            turn_id=None,
            sha256=digest,
            outcome=None,
        )

    # Turn state transitions
    # ----------------------------------------------------------------------
    #
    # ┌─────────────────┐            ┌─────────────────┐      ┌─────────────────┐
    # │    executing    │──commit───>│    completed    │      │   interrupted   │
    # └─────────────────┘            └─────────────────┘      └─────────────────┘
    #          │                                                       ^
    #          └─outcome != completed──────────────────────────────────┘
    #          │
    #          │ require_recovery
    #          v
    # ┌─────────────────┐            ┌─────────────────┐
    # │recovery_required│───────────>│    abandoned    │
    # └─────────────────┘            └─────────────────┘
    #                     reconcile
    #
    # ``fence_tool`` keeps the Turn in ``executing``; it records that one
    # tool effect may exist. A fenced unresolved Turn reports
    # ``effects_unknown`` until the host reconciles it.
    #
    # Exactly one terminal transition per Turn: completed/interrupted from
    # commit_turn, abandoned from reconcile. release_turn only drops local
    # execution ownership and never mutates stored state.
    # ----------------------------------------------------------------------

    def begin_turn(self, expected: Revision, intent: TurnIntent) -> Revision:
        self._begin(expected, Revision)
        raw = self._encode(intent, kind=TurnIntent)
        intent = decode_record(TurnIntent, raw)
        diagnostics = AttemptDiagnostics(
            binding=intent.binding, message_start=intent.message_start
        )
        diagnostic_raw = self._encode(diagnostics)
        try:
            self._admit_diagnostics(intent, diagnostics)
        except SessionStoreError:
            self._latch()
            raise
        with self._write() as conn:
            session = self._row(conn, expected)
            report = self._report(conn, expected.session_id)
            if (
                session.state != "ready"
                or session.active_turn_id is not None
                or intent.session_id != expected.session_id
                or intent.base_seq != expected.head_seq
                or intent.message_start != len(report.checkpoint.messages)
                or intent.binding != session.binding
                or instant(intent.started_at_utc)
                < instant(report.checkpoint.settled_at_utc)
            ):
                raise SessionStoreError("turn_admission_rejected")
            conn.execute(
                "INSERT INTO turns VALUES (?,?,?,?, 'executing',?,?,NULL,NULL,?)",
                (
                    intent.session_id,
                    intent.turn_id,
                    intent.base_seq,
                    intent.task_id,
                    intent.input_digest,
                    intent.started_at_utc,
                    diagnostic_raw.decode(),
                ),
            )
            revision = self._cas(
                conn,
                expected,
                state="executing",
                active=intent.turn_id,
                binding=intent.binding,
            )
        self._owned_turns.add((intent.session_id, intent.turn_id))
        return revision

    def _active(self, report: RecoveryReport, turn_id: str) -> TurnRecord:
        record = report.unresolved
        if (
            report.state != "executing"
            or record is None
            or record.state != "executing"
            or record.intent.turn_id != turn_id
            or (report.revision.session_id, turn_id) not in self._owned_turns
        ):
            raise SessionStoreError("live_turn_required")
        return record

    def fence_tool(self, expected: Revision, fence: ToolFence) -> Revision:
        self._begin(expected, Revision)
        fence = decode_record(ToolFence, self._encode(fence, kind=ToolFence))
        with self._write() as conn:
            self._fences_supported(conn)
            record = self._fence_target(conn, expected, fence.turn_id)
            previous = self._last_fence(conn, expected.session_id, fence.turn_id)
            self._check_fence_succession(conn, record, previous, fence)
            # The fence is screened here, before the INSERT, so a credential or
            # oversize row never reaches the table even though the turn's own
            # diagnostics column is no longer rewritten to hold it.
            self._encode(fence, kind=ToolFence)
            # Every admitted fence must still leave the Turn's diagnostics record
            # room for a full host resolution. Fences no longer add bytes to that
            # record (they are their own rows), so only the non-fence part is
            # measured here.
            self._admit_diagnostics(
                record.intent, self._without_fences(record.diagnostics)
            )
            conn.execute(
                "INSERT INTO tool_fences VALUES (?,?,?,?,?,?,?)",
                (
                    fence.session_id,
                    fence.turn_id,
                    fence.ordinal,
                    fence.assistant_message_index,
                    fence.call_id,
                    fence.tool_name,
                    fence.observed_at_utc,
                ),
            )
            revision = self._cas(
                conn,
                expected,
                state="executing",
                active=fence.turn_id,
                binding=record.intent.binding,
            )
        return revision

    def _fence_target(
        self, conn: sqlite3.Connection, expected: Revision, turn_id: str
    ) -> TurnRecord:
        """The locally-owned executing Turn this fence would extend.

        `_prepare` used to rebuild the whole RecoveryReport here, which is what
        rejected a corrupt Turn before any fence was written. The append path
        reads only this Turn, so it re-states the checks that report performed
        and that this Turn's row can answer: the Session must agree with the
        Turn's binding and active id, and the Turn must be the one executing.
        Anything a full report would reject must not be admitted here either.
        """
        session = self._row(conn, expected)
        record = self._turn_record_by_id(
            conn, expected.session_id, turn_id, with_fences=False
        )
        if record is None or record.state != "executing":
            raise SessionStoreError("live_turn_required")
        if (expected.session_id, turn_id) not in self._owned_turns:
            raise SessionStoreError("live_turn_required")
        if session.active_turn_id != turn_id:
            raise SessionStoreError("unresolved_turn_not_active")
        if session.state != "executing":
            raise SessionStoreError("unresolved_turn_state_mismatch")
        # `_report` compares the whole binding, not just the workspace: a Turn
        # that drifted in provider or observation time is corruption too, and
        # the CAS below would copy it onto the Session.
        if record.intent.binding != session.binding:
            raise SessionStoreError("unresolved_turn_binding_mismatch")
        # The Turn must be the base the Session points at, not merely not ahead.
        if record.intent.base_seq != expected.head_seq:
            raise SessionStoreError("record_base_chain_gap")
        # The executing-state invariants `_validate_record` enforces. Fences are
        # append-only evidence; admitting one onto a malformed Turn would bless
        # the malformation.
        if (
            record.ended_at_utc is not None
            or record.settled_seq is not None
            or record.diagnostics.reason is not None
            or record.diagnostics.resolution is not None
            or record.diagnostics.commit_version is not None
        ):
            raise SessionStoreError("invalid_relationship")
        return record

    def _turn_record_by_id(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        turn_id: str,
        *,
        with_fences: bool = True,
    ) -> TurnRecord | None:
        row = conn.execute(
            "SELECT * FROM turns WHERE session_id=? AND turn_id=?",
            (session_id, turn_id),
        ).fetchone()
        if row is None:
            return None
        return self._turn_record(conn, row, with_fences=with_fences)

    def _last_fence(
        self, conn: sqlite3.Connection, session_id: str, turn_id: str
    ) -> ToolFence | None:
        row = conn.execute(
            "SELECT * FROM tool_fences WHERE session_id=? AND turn_id=? "
            "ORDER BY ordinal DESC LIMIT 1",
            (session_id, turn_id),
        ).fetchone()
        if row is None:
            return None
        return ToolFence(
            session_id=row["session_id"],
            turn_id=row["turn_id"],
            ordinal=row["ordinal"],
            assistant_message_index=row["assistant_message_index"],
            call_id=row["call_id"],
            tool_name=row["tool_name"],
            observed_at_utc=row["observed_at_utc"],
        )

    def _check_fence_succession(
        self,
        conn: sqlite3.Connection,
        record: TurnRecord,
        previous: ToolFence | None,
        fence: ToolFence,
    ) -> None:
        """Everything the full-record validation checked, answered incrementally.

        Committed fences are immutable, so re-reading them proves nothing new;
        the only new fact is this fence. Its constraints are that it follows the
        last one and that its reference has not been used in this Turn. The
        UNIQUE constraint enforces the second on write; this check turns a
        constraint violation into the same `invalid_fence` the read path raised.
        """
        intent = record.intent
        if fence.session_id != intent.session_id or fence.turn_id != intent.turn_id:
            raise SessionStoreError("invalid_fence")
        ordinal = 1 if previous is None else previous.ordinal + 1
        if fence.ordinal != ordinal:
            raise SessionStoreError("invalid_fence")
        if previous is None:
            index, timestamp = intent.message_start, intent.started_at_utc
        else:
            index = previous.assistant_message_index
            timestamp = previous.observed_at_utc
        if fence.assistant_message_index < index:
            raise SessionStoreError("invalid_fence")
        if instant(fence.observed_at_utc) < instant(timestamp):
            raise SessionStoreError("invalid_fence")
        duplicate = conn.execute(
            "SELECT 1 FROM tool_fences WHERE session_id=? AND turn_id=? "
            "AND assistant_message_index=? AND call_id=? LIMIT 1",
            (
                fence.session_id,
                fence.turn_id,
                fence.assistant_message_index,
                fence.call_id,
            ),
        ).fetchone()
        if duplicate is not None:
            raise SessionStoreError("invalid_fence")

    def commit_turn(
        self, expected: Revision, settled: SettledTurn
    ) -> CheckpointReceipt:
        self._begin(expected, Revision)
        # Checkpoint's larger bound; everything is validated before BEGIN IMMEDIATE.
        try:
            raw_settled = self._encode(
                settled, limit=MAX_CHECKPOINT_BYTES + 32, kind=SettledTurn
            )
            settled = decode_record(
                SettledTurn, raw_settled, limit=MAX_CHECKPOINT_BYTES + 32
            )
            payload, digest = encode_checkpoint(
                settled.checkpoint, sensitive_values=self.sensitive_values
            )
            checkpoint = decode_checkpoint(payload, digest)
        except SessionStoreError:
            self._latch()
            raise
        with self._prepare(expected) as prepared:
            if checkpoint.session_id != expected.session_id:
                raise SessionStoreError("invalid_relationship")
            record = self._active(prepared, checkpoint.turn_id or "")
            # Validation needs the fences; only the JSON encoding drops them.
            # They stay in `tool_fences`, which is their single source of truth.
            diagnostics = record.diagnostics.model_copy(
                update={"commit_version": expected.version + 1}
            )
            terminal = record.model_copy(
                update={
                    "state": "completed"
                    if checkpoint.outcome == "completed"
                    else "interrupted",
                    "settled_seq": checkpoint.seq,
                    "ended_at_utc": checkpoint.settled_at_utc,
                    "diagnostics": diagnostics,
                }
            )
            self._settled_relationship(terminal, checkpoint, prepared.checkpoint)
            self._validate_record(terminal)
            format_version = 2 if self.schema_version >= 3 else 1
            payload, digest = encode_storage_checkpoint(
                checkpoint,
                prepared.receipt,
                format_version=format_version,
                sensitive_values=self.sensitive_values,
            )
        with self._write(idle_after=True) as conn:
            current = self._current(conn, expected, prepared)
            self._active(current, checkpoint.turn_id or "")
            diagnostic_raw = self._encode(self._diagnostics_for_json(conn, diagnostics))
            if format_version != (
                2 if conn.execute("PRAGMA user_version").fetchone()[0] >= 3 else 1
            ):
                raise VersionConflict()
            conn.execute(
                "INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?)",
                (
                    checkpoint.session_id,
                    checkpoint.seq,
                    checkpoint.turn_id,
                    format_version,
                    checkpoint.settled_at_utc,
                    payload,
                    digest,
                ),
            )
            conn.execute(
                "UPDATE turns SET state=?,ended_at_utc=?,settled_seq=?,diagnostics_json=? WHERE session_id=? AND turn_id=? AND state='executing'",
                (
                    terminal.state,
                    terminal.ended_at_utc,
                    terminal.settled_seq,
                    diagnostic_raw.decode(),
                    expected.session_id,
                    checkpoint.turn_id,
                ),
            )
            revision = self._cas(
                conn,
                expected,
                state="ready",
                active=None,
                binding=checkpoint.binding,
                head=checkpoint.seq,
            )
        self._owned_turns.discard((expected.session_id, checkpoint.turn_id or ""))
        return CheckpointReceipt(
            revision=revision,
            turn_id=checkpoint.turn_id,
            sha256=digest,
            outcome=checkpoint.outcome,
        )

    def require_recovery(self, expected: Revision, reason_code: Reason) -> Revision:
        self._begin(expected, Revision)
        if reason_code not in (
            "storage_failed",
            "finalization_failed",
            "binding_unavailable",
        ):
            self._latch()
            raise SessionStoreError("invalid_reason")
        with self._prepare(expected, recovery=True) as prepared:
            record = prepared.unresolved
        with self._write(recovery=True) as conn:
            self._current(conn, expected, prepared)
            session = self._row(conn, expected)
            if record is not None:
                diagnostics = self._diagnostics_for_json(
                    conn,
                    record.diagnostics.model_copy(update={"reason": reason_code}),
                )
                conn.execute(
                    "UPDATE turns SET state='recovery_required',diagnostics_json=? WHERE session_id=? AND turn_id=?",
                    (
                        self._encode(diagnostics).decode(),
                        expected.session_id,
                        record.intent.turn_id,
                    ),
                )
            revision = self._cas(
                conn,
                expected,
                state="recovery_required",
                active=session.active_turn_id,
                binding=session.binding,
            )
        self._latch()
        return revision

    def release_turn(self, expected: Revision, turn_id: str) -> None:
        """Host confirms faulted owned work AND cleanup have completely settled.

        Local-only recovery handshake, never a SQL transition or cancellation.
        The runtime calls this after run_turn's owned settlement/finalization,
        or after synchronous admission failed before any work was dispatched.
        Inspection/reconciliation or a storage failure alone proves no cleanup.
        """
        self._begin(expected, Revision)
        if type(turn_id) is not str:
            raise SessionStoreError("live_turn_required")
        try:
            uuid4_text(turn_id)
        except ValueError:
            raise SessionStoreError("live_turn_required") from None
        with self._prepare(expected, recovery=True) as prepared:
            key = (expected.session_id, turn_id)
            if key not in self._owned_turns:
                raise SessionStoreError("live_turn_required")
            if (
                expected.session_id not in self._fault_sessions
                and "*" not in self._fault_sessions
            ):
                raise SessionStoreError("turn_still_owned")
            # An uncertain COMMIT may have actually settled this exact Turn.
            record = prepared.unresolved
            if (
                record.intent.turn_id
                if record is not None
                else prepared.receipt.turn_id
            ) != turn_id:
                raise SessionStoreError("live_turn_required")
        self._owned_turns.remove(key)
        self._inspected.pop(expected.session_id, None)

    def owns_turn(self, session_id: str, turn_id: str) -> bool:
        """Local execution ownership only; stored facts never acquire it."""
        return (session_id, turn_id) in self._owned_turns

    def reconcile(self, expected: Revision, resolution: HostResolution) -> Revision:
        self._begin(expected, Revision)
        resolution = decode_record(
            HostResolution, self._encode(resolution, kind=HostResolution)
        )
        with self._prepare(expected, recovery=True) as prepared:
            if self._inspected.get(expected.session_id) != expected:
                raise SessionStoreError("fresh_inspection_required")
            record = prepared.unresolved
            if (
                resolution.session_id != expected.session_id
                or resolution.turn_id
                != (record.intent.turn_id if record is not None else None)
                or not same_workspace(prepared.binding, resolution.binding)
                or instant(resolution.resolved_at_utc)
                < instant(prepared.checkpoint.settled_at_utc)
            ):
                raise SessionStoreError("reconciliation_rejected")
            if any(sid == expected.session_id for sid, _ in self._owned_turns):
                raise SessionStoreError("turn_still_owned")
            if record is not None:
                # Validation keeps the fences; only the JSON encoding drops them,
                # and only where the fence table exists.
                diagnostics = record.diagnostics.model_copy(
                    update={"resolution": resolution}
                )
                self._validate_record(
                    record.model_copy(
                        update={
                            "state": "abandoned",
                            "ended_at_utc": resolution.resolved_at_utc,
                            "diagnostics": diagnostics,
                        }
                    )
                )
        with self._write(recovery=True, idle_after=True) as conn:
            self._current(conn, expected, prepared)
            if record is not None:
                stored = self._diagnostics_for_json(
                    conn,
                    record.diagnostics.model_copy(update={"resolution": resolution}),
                )
                conn.execute(
                    "UPDATE turns SET state='abandoned',ended_at_utc=?,diagnostics_json=? WHERE session_id=? AND turn_id=?",
                    (
                        resolution.resolved_at_utc,
                        self._encode(stored).decode(),
                        expected.session_id,
                        record.intent.turn_id,
                    ),
                )
            revision = self._cas(
                conn, expected, state="ready", active=None, binding=resolution.binding
            )
        self._inspected.pop(expected.session_id, None)
        self._fault_sessions.discard(expected.session_id)
        self._fault = bool(self._fault_sessions)
        return revision

    def close(self) -> None:
        if self.mode == "writer" and self._owned_turns:
            raise SessionStoreError("turn_still_owned")
        if self.mode == "writer" and self._owned_tasks:
            raise SessionStoreError("task_still_owned")
        if self._connection is not None:
            try:
                with self._paths.access(validate=False):
                    try:
                        self._connection.close()
                    except sqlite3.Error:
                        raise SessionStoreError("close_failed") from None
                    finally:
                        self._connection = None
            finally:
                if self._connection is None:
                    self._paths.close()
        else:
            self._paths.close()
