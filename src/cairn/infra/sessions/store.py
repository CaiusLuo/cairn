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
    encode_checkpoint,
    encode_record,
)
from cairn.infra.sessions.ledger import TaskLedgerMixin
from cairn.infra.sessions.models import (
    MAX_CHECKPOINT_BYTES,
    MAX_DATABASE_BYTES,
    MAX_INTEGER,
    MAX_RECORD_BYTES,
    MAX_WAL_BYTES,
    SCHEMA_VERSION,
    AttemptDiagnostics,
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


def sqlite_wal_fix_supported(version: tuple[int, int, int] | None = None) -> bool:
    """Release gate, not a runtime upgrade or claim that older SQLite is safe."""
    v = sqlite3.sqlite_version_info if version is None else version
    return (
        v >= (3, 51, 3)
        or (v[:2] == (3, 50) and v[2] >= 7)
        or (v[:2] == (3, 44) and v[2] >= 6)
    )


class SessionStore(TaskLedgerMixin):
    def __init__(
        self,
        directory: Path,
        *,
        mode: Literal["writer", "inspect"] = "writer",
        forbidden_roots: tuple[Path, ...],
        sensitive_values: tuple[str, ...] = (),
    ) -> None:
        if mode not in ("writer", "inspect") or any(
            type(s) is not str for s in sensitive_values
        ):
            raise SessionStoreError("invalid_options")
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
                for statement in (*_SCHEMA, *_TASK_SCHEMA):
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
        if version not in (1, SCHEMA_VERSION):
            raise SessionStoreError("schema_mismatch")
        observed = {
            " ".join(row[0].split())
            for row in conn.execute(
                "SELECT sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
            )
        }
        expected = _SCHEMA if version == 1 else (*_SCHEMA, *_TASK_SCHEMA)
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
        if version == 2:
            bounds += (("tasks", "record_json", MAX_TASK_BYTES, "text"),)
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
        self, *, recovery: bool = False, idle_after: bool = False
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
                    if self._wal_size() >= MAX_WAL_BYTES:
                        if not self._owned_turns or recovery:
                            self._maintain()
                        if self._wal_size() >= MAX_WAL_BYTES:
                            raise SessionStoreError("wal_admission_limit")
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
            diagnostics = decode_record(
                AttemptDiagnostics,
                row["diagnostics_json"].encode(),
                sensitive_values=self.sensitive_values,
            )
            record = TurnRecord(
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
            self._validate_record(record)
            result.append(record)
        return tuple(result)

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
        self, record: TurnRecord, checkpoint: CheckpointPayload
    ) -> None:
        previous = (-1, -1)
        for fence in record.diagnostics.fences:
            if fence.assistant_message_index >= len(checkpoint.messages):
                raise SessionStoreError("fenced_history_missing")
            message = checkpoint.messages[fence.assistant_message_index]
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

    def _checkpoint_row(self, row: sqlite3.Row) -> CheckpointPayload:
        """Decode one checkpoint row, enforcing its byte bound."""
        if (
            not isinstance(row["payload"], bytes)
            or len(row["payload"]) > MAX_CHECKPOINT_BYTES
        ):
            raise SessionStoreError("record_limit")
        return decode_checkpoint(
            row["payload"], row["sha256"], sensitive_values=self.sensitive_values
        )

    def _validate_checkpoint_identity(
        self,
        session: _SessionRow,
        session_id: str,
        row: sqlite3.Row,
        checkpoint: CheckpointPayload,
        previous: CheckpointPayload | None,
    ) -> None:
        """A stored checkpoint must describe the row that carries it, and follow
        its predecessor by exactly one sequence step."""
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

    def _advance_receipt(
        self,
        revision: Revision,
        session_id: str,
        checkpoint: CheckpointPayload,
        previous: CheckpointPayload | None,
        receipt: CheckpointReceipt | None,
        row: sqlite3.Row,
        by_turn: dict[str, TurnRecord],
        committed: set[str],
    ) -> CheckpointReceipt:
        """Validate the settled relationship for a later checkpoint and return
        the receipt it advances to."""
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
        return CheckpointReceipt(
            revision=Revision(
                session_id=session_id,
                version=commit_version,
                head_seq=checkpoint.seq,
            ),
            turn_id=checkpoint.turn_id,
            sha256=row["sha256"],
            outcome=checkpoint.outcome,
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

    def _validate_unresolved_intent(
        self,
        session: _SessionRow,
        revision: Revision,
        unresolved: Sequence[TurnRecord],
        previous: CheckpointPayload,
    ) -> None:
        """An unresolved turn must continue from the committed head."""
        if not unresolved:
            return
        active = unresolved[0]
        if active.intent.base_seq != revision.head_seq:
            raise SessionStoreError("unresolved_turn_base_not_head")
        if active.intent.message_start != len(previous.messages):
            raise SessionStoreError("unresolved_turn_message_start_mismatch")
        if active.intent.binding != session.binding:
            raise SessionStoreError("unresolved_turn_binding_mismatch")

    def _recovery_status(
        self, session: _SessionRow, unresolved: Sequence[TurnRecord]
    ) -> Literal["clean", "executing", "effects_unknown", "recovery_required"]:
        if session.state == "recovery_required" or self._fault:
            return "recovery_required"
        if not unresolved:
            return "clean"
        return "effects_unknown" if unresolved[0].diagnostics.fences else "executing"

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
        previous: CheckpointPayload | None = None
        receipt: CheckpointReceipt | None = None
        committed: set[str] = set()
        bases: dict[int, tuple[int, str]] = {}
        for row in conn.execute(
            "SELECT * FROM checkpoints WHERE session_id=? ORDER BY seq", (session_id,)
        ):
            checkpoint = self._checkpoint_row(row)
            self._validate_checkpoint_identity(
                session, session_id, row, checkpoint, previous
            )
            receipt = self._advance_receipt(
                revision,
                session_id,
                checkpoint,
                previous,
                receipt,
                row,
                by_turn,
                committed,
            )
            previous = checkpoint
            bases[checkpoint.seq] = (
                len(checkpoint.messages),
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
        self._validate_unresolved_intent(session, revision, unresolved, previous)
        return RecoveryReport(
            revision=revision,
            state=session.state,
            status=self._recovery_status(session, unresolved),
            binding=session.binding,
            checkpoint=previous,
            receipt=receipt,
            attempts=records,
            local_fault=self._fault,
        )

    def _settled_relationship(
        self,
        record: TurnRecord,
        checkpoint: CheckpointPayload,
        previous: CheckpointPayload,
    ) -> None:
        intent = record.intent
        if (
            checkpoint.seq != intent.base_seq + 1
            or checkpoint.turn_id != intent.turn_id
            or checkpoint.message_range.start != intent.message_start
            or intent.message_start != len(previous.messages)
            or checkpoint.messages[: len(previous.messages)] != previous.messages
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
        tail = checkpoint.messages[intent.message_start :]
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

    def _validate_sessions(self, conn: sqlite3.Connection) -> None:
        for row in conn.execute("SELECT session_id FROM sessions"):
            self._report(conn, row[0])

    def _backup_v1(self, conn: sqlite3.Connection) -> Path:
        path = self._paths.create_backup()
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
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def upgrade_schema(self) -> Path:
        """Explicit trusted-host v1→v2 operation; retain a consistent v1 backup.

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
                if conn.execute("PRAGMA user_version").fetchone()[0] != 1:
                    raise SessionStoreError("schema_upgrade_not_required")
                self._validate_sessions(conn)
                backup = self._backup_v1(conn)
            with self._write(idle_after=True) as conn:
                if conn.execute("PRAGMA user_version").fetchone()[0] != 1:
                    raise SessionStoreError("schema_mismatch")
                self._migrate_v1(conn)
                self._schema(conn)
                self._validate_sessions(conn)
            return backup
        except SessionStoreError:
            raise
        except Exception:
            self._latch()
            raise SessionStoreError("schema_upgrade_failed") from None

    def _tasks_supported(self, conn: sqlite3.Connection) -> None:
        if conn.execute("PRAGMA user_version").fetchone()[0] != 2:
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
        self._row(conn, expected)
        current = self._report(conn, expected.session_id)
        if current != prepared:
            raise VersionConflict()
        return current

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
        payload, digest = encode_checkpoint(
            checkpoint, sensitive_values=self.sensitive_values
        )
        with self._write(idle_after=True) as conn:
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
                "INSERT INTO checkpoints VALUES (?,0,NULL,1,?,?,?)",
                (binding.session_id, checkpoint.settled_at_utc, payload, digest),
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
        with self._prepare(expected) as prepared:
            record = self._active(prepared, fence.turn_id)
            if (
                fence.session_id != expected.session_id
                or fence.turn_id != record.intent.turn_id
            ):
                raise SessionStoreError("invalid_fence")
            diagnostics = record.diagnostics.model_copy(
                update={"fences": (*record.diagnostics.fences, fence)}
            )
            diagnostic_raw = self._encode(diagnostics)
            self._validate_record(
                record.model_copy(update={"diagnostics": diagnostics})
            )
            self._admit_diagnostics(record.intent, diagnostics)
        with self._write() as conn:
            current = self._current(conn, expected, prepared)
            self._active(current, fence.turn_id)
            conn.execute(
                "UPDATE turns SET diagnostics_json=? WHERE session_id=? AND turn_id=? AND state='executing'",
                (diagnostic_raw.decode(), expected.session_id, fence.turn_id),
            )
            revision = self._cas(
                conn,
                expected,
                state="executing",
                active=fence.turn_id,
                binding=record.intent.binding,
            )
        return revision

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
            diagnostic_raw = self._encode(diagnostics)
            self._settled_relationship(terminal, checkpoint, prepared.checkpoint)
            self._validate_record(terminal)
        with self._write(idle_after=True) as conn:
            current = self._current(conn, expected, prepared)
            self._active(current, checkpoint.turn_id or "")
            conn.execute(
                "INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?)",
                (
                    checkpoint.session_id,
                    checkpoint.seq,
                    checkpoint.turn_id,
                    checkpoint.format_version,
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
            raw: bytes | None = None
            if record is not None:
                diagnostics = record.diagnostics.model_copy(
                    update={"reason": reason_code}
                )
                raw = self._encode(diagnostics)
        with self._write(recovery=True) as conn:
            self._current(conn, expected, prepared)
            session = self._row(conn, expected)
            if record is not None and raw is not None:
                conn.execute(
                    "UPDATE turns SET state='recovery_required',diagnostics_json=? WHERE session_id=? AND turn_id=?",
                    (raw.decode(), expected.session_id, record.intent.turn_id),
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
            raw: bytes | None = None
            if record is not None:
                diagnostics = record.diagnostics.model_copy(
                    update={"resolution": resolution}
                )
                abandoned = record.model_copy(
                    update={
                        "state": "abandoned",
                        "ended_at_utc": resolution.resolved_at_utc,
                        "diagnostics": diagnostics,
                    }
                )
                raw = self._encode(diagnostics)
                self._validate_record(abandoned)
        with self._write(recovery=True, idle_after=True) as conn:
            self._current(conn, expected, prepared)
            if record is not None and raw is not None:
                conn.execute(
                    "UPDATE turns SET state='abandoned',ended_at_utc=?,diagnostics_json=? WHERE session_id=? AND turn_id=?",
                    (
                        resolution.resolved_at_utc,
                        raw.decode(),
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
