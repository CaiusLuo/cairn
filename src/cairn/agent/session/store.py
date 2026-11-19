"""Concrete, host-only PostgreSQL store. Facts in this database never dispatch work."""

import hashlib
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from psycopg import Error as PostgresError

from cairn.agent.session.codec import (
    canonical,
    decode_checkpoint,
    decode_record,
    decode_storage_checkpoint,
    encode_checkpoint,
    encode_record,
    encode_storage_checkpoint,
)
from cairn.agent.session.ledger import TaskLedgerMixin
from cairn.agent.session.models import (
    MAX_CHECKPOINT_BYTES,
    MAX_INTEGER,
    MAX_LISTED_SESSIONS,
    MAX_MESSAGES,
    MAX_RECORD_BYTES,
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
    SessionSummary,
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
from cairn.agent.session.task_models import TaskRevision
from cairn.shared.db import Database, DbConnection, Row


class _SessionRow(StrictDTO):
    revision: Revision
    state: SessionState
    active_turn_id: str | None
    binding: SessionBinding
    created_at_utc: str
    updated_at_utc: str


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


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
        database: Database,
        *,
        mode: Literal["writer", "inspect"] = "writer",
        sensitive_values: tuple[str, ...] = (),
    ) -> None:
        if mode not in ("writer", "inspect") or any(
            type(s) is not str for s in sensitive_values
        ):
            raise SessionStoreError("invalid_options")
        self.db = database
        self.mode = mode
        self.sensitive_values = sensitive_values
        self._connection: DbConnection | None = None
        self._fault = False
        self._fault_sessions: set[str] = set()
        self._recovery_sessions: set[str] = set()
        self._mutation_session = "*"
        self._owned_turns: set[tuple[str, str]] = set()
        self._owned_tasks: set[tuple[str, str]] = set()
        self._inspected: dict[str, Revision] = {}
        self.maintenance_failed = False
        try:
            self._connection = database.dedicated("agent")
        except PostgresError:
            self.close()
            raise SessionStoreError("invalid_or_unavailable_store") from None

    def _conn(self) -> DbConnection:
        if self._connection is None:
            raise SessionStoreError("closed_store")
        return self._connection

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
        self, conn: DbConnection, diagnostics: AttemptDiagnostics
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

    @contextmanager
    def _write(
        self,
        *,
        recovery: bool = False,
        idle_after: bool = False,
    ) -> Iterator[DbConnection]:
        del idle_after
        if self.mode != "writer":
            raise SessionStoreError("read_only_store")
        if self._fault and not recovery:
            raise SessionStoreError("recovery_required")
        conn = self._conn()
        committing = False
        try:
            conn.execute("BEGIN")
            yield conn
            committing = True
            self._commit(conn)
        except BaseException as exc:
            conn.rollback()
            self._latch()
            if isinstance(exc, SessionStoreError):
                raise
            if isinstance(exc, Exception):
                raise SessionStoreError(
                    "commit_uncertain" if committing else "storage_failed"
                ) from None
            raise

    @staticmethod
    def _commit(conn: DbConnection) -> None:
        """Commit seam so tests can exercise an uncertain outcome."""
        conn.commit()

    def _row(self, conn: DbConnection, expected: Revision) -> _SessionRow:
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id=%s", (expected.session_id,)
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
        conn: DbConnection,
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
            "UPDATE sessions SET version=%s,head_seq=%s,state=%s,active_turn_id=%s,binding_json=%s,updated_at_utc=%s WHERE session_id=%s AND version=%s AND head_seq=%s",
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

    def _records(self, conn: DbConnection, session_id: str) -> tuple[TurnRecord, ...]:
        result: list[TurnRecord] = []
        for row in conn.execute(
            "SELECT * FROM turns WHERE session_id=%s ORDER BY seq", (session_id,)
        ):
            record = self._turn_record(conn, row)
            self._validate_record(record)
            result.append(record)
        return tuple(result)

    def _turn_record(
        self, conn: DbConnection, row: Row, *, with_fences: bool = True
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
    def _has_fence_table(conn: DbConnection) -> bool:
        del conn
        return True

    def _fences_for(
        self, conn: DbConnection, session_id: str, turn_id: str
    ) -> tuple[ToolFence, ...]:
        # A legacy schema has no fence table; its fences are still in the JSON
        # column and `decode_record` above has already loaded them.
        if not self._has_fence_table(conn):
            return ()
        rows = conn.execute(
            "SELECT * FROM tool_fences WHERE session_id=%s AND turn_id=%s ORDER BY ordinal",
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

    def _checkpoint_row(self, row: Row) -> CheckpointPayload | CheckpointDelta:
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

    def _report(self, conn: DbConnection, session_id: str) -> RecoveryReport:
        raw = conn.execute(
            "SELECT version,head_seq FROM sessions WHERE session_id=%s", (session_id,)
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
            "SELECT * FROM checkpoints WHERE session_id=%s ORDER BY seq", (session_id,)
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
            local_fault=self._fault or session_id in self._recovery_sessions,
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
    def _read(self) -> Iterator[DbConnection]:
        conn = self._conn()
        try:
            conn.execute("BEGIN READ ONLY" if self.mode == "inspect" else "BEGIN")
            yield conn
        except (SessionStoreError, VersionConflict):
            raise
        except (PostgresError, ValueError, TypeError, KeyError, AttributeError):
            raise SessionStoreError("invalid_or_unavailable_store") from None
        finally:
            conn.rollback()

    def _snapshot(self, session_id: str) -> RecoveryReport:
        with self._read() as conn:
            uuid4_text(session_id)
            return self._report(conn, session_id)

    def inspect(self, session_id: str) -> RecoveryReport:
        report = self._snapshot(session_id)
        self._inspected[session_id] = report.revision
        return report

    def list_sessions(
        self, *, limit: int = MAX_LISTED_SESSIONS
    ) -> list[SessionSummary]:
        """Recoverable Sessions, most recently updated first.

        This reads the Session row only. Listing must stay cheap enough to run
        before a caller has decided anything, so no checkpoint is decoded and no
        attempt is replayed here; ``inspect`` is the call that answers whether a
        given Session can be reopened.
        """
        if type(limit) is not int or limit < 1:
            raise SessionStoreError("invalid_options")
        with self._read() as conn:
            rows = conn.execute(
                "SELECT session_id, state, created_at_utc, updated_at_utc, head_seq "
                "FROM sessions ORDER BY updated_at_utc DESC, session_id ASC LIMIT %s",
                (limit,),
            ).fetchall()
            return [
                SessionSummary(
                    session_id=row[0],
                    state=row[1],
                    created_at_utc=row[2],
                    updated_at_utc=row[3],
                    head_seq=row[4],
                )
                for row in rows
            ]

    def matching_sessions(self, prefix: str) -> list[SessionSummary]:
        """Stored Sessions whose identifier starts with ``prefix``.

        An abbreviated identifier is matched with and without hyphens. A whole
        identifier is matched exactly, so it never depends on a scan. Candidates
        are returned for the caller to choose from; selecting one is the caller's
        decision, which is why this returns a list rather than asserting
        uniqueness.
        """
        if not isinstance(prefix, str) or not prefix:
            raise SessionStoreError("invalid_options")
        digits = prefix.replace("-", "")
        if not digits or any(c not in "0123456789abcdef" for c in digits):
            raise SessionStoreError("invalid_session_id")
        if len(digits) > 32:
            raise SessionStoreError("invalid_session_id")
        if len(digits) == 32:
            # A whole identifier is unambiguous by construction, so it needs no
            # scan. Stored identities are canonical UUID text; a caller who omits
            # the hyphens still means the same Session.
            canonical = str(UUID(digits))
            uuid4_text(canonical)
            clause = "WHERE session_id = %s"
            parameters: tuple[str, ...] = (canonical,)
        else:
            # A partial identifier is matched against the stored form as typed,
            # and against its hyphenated form when the caller typed digits only.
            clause = "WHERE session_id LIKE %s OR session_id LIKE %s"
            parameters = (f"{prefix}%", f"{digits}%")
        with self._read() as conn:
            rows = conn.execute(
                "SELECT session_id, state, created_at_utc, updated_at_utc, head_seq "
                f"FROM sessions {clause} "
                "ORDER BY updated_at_utc DESC, session_id ASC",
                parameters,
            ).fetchall()
        return [
            SessionSummary(
                session_id=row[0],
                state=row[1],
                created_at_utc=row[2],
                updated_at_utc=row[3],
                head_seq=row[4],
            )
            for row in rows
        ]

    def resolve_session_id(self, prefix: str) -> str:
        """The one Session whose identifier starts with ``prefix``.

        Refuses an ambiguous prefix rather than picking one, because reopening
        the wrong conversation resumes work against the wrong history. A caller
        that wants to offer a choice uses ``matching_sessions`` instead of
        handling that refusal.
        """
        matches = self.matching_sessions(prefix)
        if not matches:
            raise SessionStoreError("session_not_found")
        if len(matches) > 1:
            raise SessionStoreError("ambiguous_session_id")
        return matches[0].session_id

    @property
    def schema_version(self) -> int:
        return SCHEMA_VERSION

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
        self, conn: DbConnection, expected: Revision, prepared: RecoveryReport
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
            conn.execute(
                "INSERT INTO sessions VALUES (%s,%s,%s,'ready',NULL,%s,%s,%s)",
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
                "INSERT INTO checkpoints VALUES (%s,0,NULL,%s,%s,%s,%s)",
                (
                    binding.session_id,
                    format_version,
                    checkpoint.settled_at_utc,
                    payload,
                    digest,
                ),
            )
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
                "INSERT INTO turns (session_id,turn_id,base_seq,task_id,state,input_digest, "
                "started_at_utc,ended_at_utc,settled_seq,diagnostics_json) "
                "VALUES (%s,%s,%s,%s, 'executing',%s,%s,NULL,NULL,%s)",
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
                "INSERT INTO tool_fences VALUES (%s,%s,%s,%s,%s,%s,%s)",
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
        self, conn: DbConnection, expected: Revision, turn_id: str
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
        conn: DbConnection,
        session_id: str,
        turn_id: str,
        *,
        with_fences: bool = True,
    ) -> TurnRecord | None:
        row = conn.execute(
            "SELECT * FROM turns WHERE session_id=%s AND turn_id=%s",
            (session_id, turn_id),
        ).fetchone()
        if row is None:
            return None
        return self._turn_record(conn, row, with_fences=with_fences)

    def _last_fence(
        self, conn: DbConnection, session_id: str, turn_id: str
    ) -> ToolFence | None:
        row = conn.execute(
            "SELECT * FROM tool_fences WHERE session_id=%s AND turn_id=%s "
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
        conn: DbConnection,
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
            "SELECT 1 FROM tool_fences WHERE session_id=%s AND turn_id=%s "
            "AND assistant_message_index=%s AND call_id=%s LIMIT 1",
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
            conn.execute(
                "INSERT INTO checkpoints VALUES (%s,%s,%s,%s,%s,%s,%s)",
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
                "UPDATE turns SET state=%s,ended_at_utc=%s,settled_seq=%s,diagnostics_json=%s WHERE session_id=%s AND turn_id=%s AND state='executing'",
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
                    "UPDATE turns SET state='recovery_required',diagnostics_json=%s WHERE session_id=%s AND turn_id=%s",
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
        # A successfully persisted recovery requirement belongs to this Session.
        # A pre-existing codec/transaction fault remains store-wide and cannot be
        # cleared by recording an otherwise valid recovery marker.
        self._recovery_sessions.add(expected.session_id)
        self._inspected.pop(expected.session_id, None)
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
                and expected.session_id not in self._recovery_sessions
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
                    "UPDATE turns SET state='abandoned',ended_at_utc=%s,diagnostics_json=%s WHERE session_id=%s AND turn_id=%s",
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
        self._recovery_sessions.discard(expected.session_id)
        self._fault = bool(self._fault_sessions)
        return revision

    def close(self) -> None:
        if self.mode == "writer" and self._owned_turns:
            raise SessionStoreError("turn_still_owned")
        if self.mode == "writer" and self._owned_tasks:
            raise SessionStoreError("task_still_owned")
        if self._connection is not None:
            self._connection.close()
            self._connection = None
