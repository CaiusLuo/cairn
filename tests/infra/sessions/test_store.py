import sqlite3
from pathlib import Path

import pytest

from cairn.core.models import Message
from cairn.infra.sessions.models import (
    HostResolution,
    Outcome,
    ProviderReference,
    SessionStoreError,
    ToolFence,
    VersionConflict,
)
from tests.support.sessions import (
    OTHER_TURN,
    SESSION,
    T4,
    binding,
    fence,
    intent,
    messages,
    settled,
    store,
)


def test_create_begin_fence_commit_inspect_and_reconcile(tmp_path: Path) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    try:
        initial = api.create(bind)
        assert (initial.revision.version, initial.revision.head_seq) == (0, 0)
        assert api.inspect(SESSION).status == "clean"
        turn = intent(bind, initial.revision)
        started = api.begin_turn(initial.revision, turn)
        fenced = api.fence_tool(started, fence(bind))
        assert (started.version, fenced.version) == (1, 2)
        assert api.inspect(SESSION).status == "effects_unknown"
        receipt = api.commit_turn(fenced, settled(turn))
        assert (receipt.revision.version, receipt.revision.head_seq) == (3, 1)
        report = api.inspect(SESSION)
        assert report.status == "clean"
        assert report.receipt == receipt
        assert len(report.checkpoint.messages) == 4
        revision = api.reconcile(
            report.revision,
            HostResolution(
                session_id=SESSION,
                turn_id=None,
                actor="test host",
                resolved_at_utc=T4,
                binding=report.binding,
            ),
        )
        assert (revision.version, revision.head_seq) == (4, 1)
        assert api.inspect(SESSION).receipt == receipt
    finally:
        api.close()
    reader = store(tmp_path, mode="inspect")
    try:
        assert reader.inspect(SESSION).revision == revision
        assert reader.inspect(SESSION).receipt == receipt
    finally:
        reader.close()


@pytest.mark.parametrize(
    "operation", ["begin", "fence", "commit", "recovery", "reconcile"]
)
def test_stale_cas_never_advances_rows_and_latches_gate(
    tmp_path: Path, operation: str
) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    try:
        initial = api.create(bind).revision
        api.inspect(SESSION)
        turn = intent(bind, initial)
        active = api.begin_turn(initial, turn)
        with pytest.raises(VersionConflict):
            if operation == "begin":
                api.begin_turn(initial, intent(bind, initial, turn_id=OTHER_TURN))
            elif operation == "fence":
                api.fence_tool(initial, fence(bind))
            elif operation == "commit":
                api.commit_turn(initial, settled(turn))
            elif operation == "recovery":
                api.require_recovery(initial, "storage_failed")
            else:
                # Freshness and revision checks both fail closed. This report is
                # from the old revision, before the now-active turn.
                api.reconcile(
                    initial,
                    HostResolution(
                        session_id=SESSION,
                        turn_id=None,
                        actor="host",
                        resolved_at_utc=T4,
                        binding=bind,
                    ),
                )
        report = api.inspect(SESSION)
        assert report.revision == active
        assert report.checkpoint.seq == 0
        assert report.unresolved is not None
        assert report.local_fault
        with pytest.raises(SessionStoreError, match="recovery_required"):
            api.commit_turn(active, settled(turn))
    finally:
        api.close()


@pytest.mark.parametrize("operation", ["fence", "commit"])
def test_duplicate_mutations_never_repeat_or_clear_fault(
    tmp_path: Path, operation: str
) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    try:
        initial = api.create(bind).revision
        turn = intent(bind, initial)
        active = api.begin_turn(initial, turn)
        fenced = api.fence_tool(active, fence(bind))
        expected = fenced
        if operation == "commit":
            expected = api.commit_turn(fenced, settled(turn)).revision
        with pytest.raises(SessionStoreError):
            if operation == "fence":
                api.fence_tool(fenced, fence(bind))
            else:
                api.commit_turn(expected, settled(turn))
        report = api.inspect(SESSION)
        assert report.revision == expected
        assert report.checkpoint.seq == (1 if operation == "commit" else 0)
        assert len(report.attempts[0].diagnostics.fences) == 1
    finally:
        api.close()


def test_reopened_attempt_requires_exact_host_abandonment(tmp_path: Path) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    initial = api.create(bind).revision
    turn = intent(bind, initial)
    active = api.begin_turn(initial, turn)
    fenced = api.fence_tool(active, fence(bind))
    api.close()
    api = store(tmp_path)
    try:
        report = api.inspect(SESSION)
        assert report.status == "effects_unknown"
        with pytest.raises(SessionStoreError, match="live_turn_required"):
            api.commit_turn(fenced, settled(turn))
        report = api.inspect(SESSION)
        marked = api.require_recovery(report.revision, "storage_failed")
        assert marked.version == fenced.version + 1
        report = api.inspect(SESSION)
        assert report.status == "recovery_required"
        resolution = HostResolution(
            session_id=SESSION,
            turn_id=turn.turn_id,
            actor="human host",
            resolved_at_utc=T4,
            binding=bind,
            note="Inspected possible effects; abandon old request.",
        )
        ready = api.reconcile(marked, resolution)
        assert (ready.version, ready.head_seq) == (4, 0)
        report = api.inspect(SESSION)
        assert report.status == "clean"
        assert report.checkpoint.messages == ()
        assert report.attempts[0].state == "abandoned"
        assert report.attempts[0].diagnostics.fences == (fence(bind),)
        assert report.attempts[0].diagnostics.resolution == resolution
        with pytest.raises(SessionStoreError):
            api.begin_turn(ready, intent(bind, ready))  # old Turn ID is never reused
    finally:
        api.close()


def test_clean_reconciliation_can_acknowledge_provider_but_not_replaced_root(
    tmp_path: Path,
) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    try:
        api.create(bind)
        report = api.inspect(SESSION)
        new = bind.model_copy(
            update={
                "provider": ProviderReference(
                    provider="offline",
                    model_group="test",
                    model_ids=("test-model",),
                    config_digest="1" * 64,
                    system_prompt_digest="2" * 64,
                    context_budget=1000,
                    output_reserve=100,
                    max_steps=20,
                )
            }
        )
        resolved = api.reconcile(
            report.revision,
            HostResolution(
                session_id=SESSION,
                turn_id=None,
                actor="host",
                resolved_at_utc=T4,
                binding=new,
            ),
        )
        current = api.inspect(SESSION)
        assert current.binding.provider == new.provider
        assert current.checkpoint.binding.provider is None
        changed = new.model_copy(
            update={
                "workspace": new.workspace.model_copy(
                    update={
                        "root": new.workspace.root.model_copy(
                            update={"inode": new.workspace.root.inode + 1}
                        )
                    }
                )
            }
        )
        with pytest.raises(SessionStoreError, match="reconciliation_rejected"):
            api.reconcile(
                resolved,
                HostResolution(
                    session_id=SESSION,
                    turn_id=None,
                    actor="host",
                    resolved_at_utc=T4,
                    binding=changed,
                ),
            )
        assert api.inspect(SESSION).revision == resolved
    finally:
        api.close()


@pytest.mark.parametrize(
    "damage",
    [
        "prefix",
        "range",
        "input",
        "fence_name",
        "fence_index",
        "fence_order",
        "binding",
        "provider",
        "seq",
    ],
)
def test_history_input_binding_and_fences_must_match(
    tmp_path: Path, damage: str
) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    try:
        initial = api.create(bind).revision
        first = intent(bind, initial)
        active = api.begin_turn(initial, first)
        receipt = api.commit_turn(active, settled(first))
        report = api.inspect(SESSION)
        second = intent(
            report.binding, receipt.revision, start=4, turn_id=OTHER_TURN, time=T4
        )
        active = api.begin_turn(receipt.revision, second)
        if damage in ("fence_name", "fence_index", "fence_order"):
            active = api.fence_tool(
                active,
                fence(
                    bind,
                    turn_id=OTHER_TURN,
                    index=5 if damage != "fence_index" else 6,
                    tool_name="edit_file" if damage == "fence_name" else "read_file",
                ).model_copy(update={"observed_at_utc": T4}),
            )
        history = messages() + messages()
        if damage == "prefix":
            history[2].content = "retroactively changed"
        elif damage == "input":
            history[4].content = "different request"
        elif damage == "fence_order":
            history[5].tool_calls.reverse()
            # Its sole fenced call is removed, rather than creating a dangling group.
            history[5].tool_calls[0].id = "other"
            history[6].tool_call_id = "other"
        value = settled(second, history=history)
        checkpoint = value.checkpoint
        if damage == "range":
            checkpoint = checkpoint.model_copy(
                update={
                    "message_range": checkpoint.message_range.model_copy(
                        update={"start": 0}
                    )
                }
            )
        elif damage == "binding":
            checkpoint = checkpoint.model_copy(
                update={
                    "binding": checkpoint.binding.model_copy(
                        update={
                            "workspace": bind.workspace.model_copy(
                                update={
                                    "root": bind.workspace.root.model_copy(
                                        update={"inode": 123}
                                    )
                                }
                            )
                        }
                    )
                }
            )
        elif damage == "provider":
            checkpoint = checkpoint.model_copy(
                update={
                    "binding": checkpoint.binding.model_copy(
                        update={
                            "provider": ProviderReference(
                                provider="different",
                                model_group="test",
                                model_ids=("model",),
                                config_digest="0" * 64,
                                system_prompt_digest="0" * 64,
                                context_budget=100,
                                output_reserve=10,
                                max_steps=1,
                            )
                        }
                    )
                }
            )
        elif damage == "seq":
            checkpoint = checkpoint.model_copy(update={"seq": 3})
        with pytest.raises(SessionStoreError):
            api.commit_turn(active, value.model_copy(update={"checkpoint": checkpoint}))
        current = api.inspect(SESSION)
        assert current.revision == active
        assert current.checkpoint == report.checkpoint
        assert report.checkpoint.messages[2].content == "observed"
    finally:
        api.close()


@pytest.mark.parametrize("outcome", ["cancelled", "budget_exhausted", "runtime_error"])
def test_settled_interruptions_empty_rollback_and_closed_groups(
    tmp_path: Path, outcome: Outcome
) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    try:
        initial = api.create(bind).revision
        turn = intent(bind, initial)
        active = api.begin_turn(initial, turn)
        receipt = api.commit_turn(active, settled(turn, history=[], outcome=outcome))
        assert api.inspect(SESSION).checkpoint.messages == ()
        turn = intent(
            api.inspect(SESSION).binding, receipt.revision, turn_id=OTHER_TURN, time=T4
        )
        active = api.begin_turn(receipt.revision, turn)
        history = messages()[:3]
        receipt = api.commit_turn(
            active, settled(turn, history=history, outcome=outcome)
        )
        report = api.inspect(SESSION)
        assert report.receipt == receipt
        assert report.attempts[-1].state == "interrupted"
        assert report.checkpoint.messages[-1].role == "tool"
    finally:
        api.close()


@pytest.mark.parametrize("after", ["checkpoint", "attempt"])
def test_terminal_transaction_failure_rolls_back_every_row(
    tmp_path: Path, after: str
) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    try:
        initial = api.create(bind).revision
        turn = intent(bind, initial)
        active = api.begin_turn(initial, turn)
        assert api._connection is not None

        def authorize(
            action: int,
            table: str | None,
            column: str | None,
            database: str | None,
            source: str | None,
        ) -> int:
            blocked = "turns" if after == "checkpoint" else "sessions"
            return (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_UPDATE and table == blocked
                else sqlite3.SQLITE_OK
            )

        api._connection.set_authorizer(authorize)
        with pytest.raises(SessionStoreError, match="storage_failed"):
            api.commit_turn(active, settled(turn))
        api._connection.set_authorizer(None)
        report = api.inspect(SESSION)
        assert report.revision == active
        assert report.checkpoint.seq == 0
        assert report.unresolved is not None and report.unresolved.state == "executing"
        with sqlite3.connect(api.path) as conn:
            assert conn.execute("SELECT count(*) FROM checkpoints").fetchone() == (1,)
            assert conn.execute(
                "SELECT settled_seq,ended_at_utc FROM turns"
            ).fetchone() == (None, None)
        with pytest.raises(SessionStoreError, match="recovery_required"):
            api.commit_turn(active, settled(turn))
    finally:
        api.close()


@pytest.mark.parametrize("committed", [False, True])
def test_uncertain_commit_is_only_confirmed_by_exact_inspected_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, committed: bool
) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    try:
        initial = api.create(bind).revision
        turn = intent(bind, initial)
        active = api.begin_turn(initial, turn)

        def uncertain(conn: sqlite3.Connection) -> None:
            if committed:
                conn.commit()
            raise sqlite3.OperationalError("provider-secret-and-sql-payload")

        monkeypatch.setattr(api, "_commit", uncertain)
        with pytest.raises(SessionStoreError, match="commit_uncertain") as error:
            api.commit_turn(active, settled(turn))
        assert "provider-secret" not in str(error.value)
        report = api.inspect(SESSION)
        assert report.local_fault
        assert report.checkpoint.seq == int(committed)
        assert report.receipt.turn_id == (turn.turn_id if committed else None)
        assert report.revision.version == active.version + int(committed)
        with pytest.raises(SessionStoreError, match="recovery_required"):
            api.commit_turn(report.revision, settled(turn))
    finally:
        api.close()


def test_sensitive_and_oversize_rejected_before_begin_and_absent_from_db_wal(
    tmp_path: Path,
) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path, sensitive_values=("opaque-sensitive-哨兵",))
    try:
        initial = api.create(bind).revision
        turn = intent(bind, initial)
        active = api.begin_turn(initial, turn)
        with pytest.raises(SessionStoreError, match="sensitive"):
            api.fence_tool(active, fence(bind, tool_name="opaque-sensitive-哨兵"))
        report = api.inspect(SESSION)
        api.release_turn(report.revision, turn.turn_id)
        report = api.inspect(SESSION)
        api.reconcile(
            report.revision,
            HostResolution(
                session_id=SESSION,
                turn_id=turn.turn_id,
                actor="host",
                resolved_at_utc=T4,
                binding=bind,
            ),
        )
        report = api.inspect(SESSION)
        turn = intent(bind, report.revision, turn_id=OTHER_TURN, time=T4)
        active = api.begin_turn(report.revision, turn)
        for text in ("opaque-sensitive-哨兵", "x" * (1024 * 1024 + 1)):
            assert api._connection is not None
            statements: list[str] = []
            api._connection.set_trace_callback(statements.append)
            history = [
                Message(role="user", content="hello"),
                Message(role="assistant", content=text),
            ]
            with pytest.raises(SessionStoreError):
                api.commit_turn(active, settled(turn, history=history))
            api._connection.set_trace_callback(None)
            assert not any(s.startswith("BEGIN") for s in statements)
        for path in api.directory.iterdir():
            assert "opaque-sensitive-哨兵".encode() not in path.read_bytes()
            assert b"x" * 4096 not in path.read_bytes()
        assert api.inspect(SESSION).revision == active
    finally:
        api.close()


def test_busy_transaction_has_bounded_timeout_and_rolls_back(tmp_path: Path) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    blocker = sqlite3.connect(api.path, isolation_level=None)
    try:
        initial = api.create(bind).revision
        assert api._connection is not None
        assert api._connection.execute("PRAGMA busy_timeout").fetchone()[0] == 2000
        assert api._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert api._connection.execute("PRAGMA trusted_schema").fetchone()[0] == 0
        assert api._connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        api._connection.execute(
            "PRAGMA busy_timeout=1"
        )  # same deterministic busy path, quick test
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(SessionStoreError, match="storage_failed"):
            api.begin_turn(initial, intent(bind, initial))
        blocker.rollback()
        report = api.inspect(SESSION)
        assert report.revision == initial and report.attempts == ()
    finally:
        blocker.close()
        api.close()


def test_sqlite_full_rejects_terminal_insert_without_partial_state(
    tmp_path: Path,
) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    try:
        initial = api.create(bind).revision
        turn = intent(bind, initial)
        active = api.begin_turn(initial, turn)
        assert api._connection is not None
        pages = api._connection.execute("PRAGMA page_count").fetchone()[0]
        api._connection.execute(f"PRAGMA max_page_count={pages}")
        history = [
            Message(role="user", content="hello"),
            Message(role="assistant", content="x" * 500_000),
        ]
        with pytest.raises(SessionStoreError, match="storage_failed"):
            api.commit_turn(active, settled(turn, history=history))
        report = api.inspect(SESSION)
        assert report.revision == active
        assert report.checkpoint.seq == 0 and report.unresolved is not None
        assert report.unresolved.settled_seq is None
    finally:
        api.close()


def test_revalidation_on_read_rejects_newly_known_credentials(tmp_path: Path) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    initial = api.create(bind).revision
    turn = intent(bind, initial)
    active = api.begin_turn(initial, turn)
    history = messages()
    history[2].content = "newly-known-opaque-credential"
    api.commit_turn(active, settled(turn, history=history))
    api.close()
    reader = store(
        tmp_path, mode="inspect", sensitive_values=("newly-known-opaque-credential",)
    )
    try:
        with pytest.raises(SessionStoreError, match="sensitive_record") as error:
            reader.inspect(SESSION)
        assert "newly-known-opaque-credential" not in str(error.value)
        assert reader.path.exists()
    finally:
        reader.close()


def test_failed_fence_subclass_and_invalid_settled_container_latch_gate(
    tmp_path: Path,
) -> None:
    class UnsupportedFence(ToolFence):
        extra: str

    bind = binding(tmp_path)
    api = store(tmp_path)
    try:
        initial = api.create(bind).revision
        turn = intent(bind, initial)
        active = api.begin_turn(initial, turn)
        extended = UnsupportedFence(
            **fence(bind).model_dump(), extra="no silent field omission"
        )
        with pytest.raises(SessionStoreError):
            api.fence_tool(active, extended)
        report = api.inspect(SESSION)
        assert report.local_fault
        assert (
            report.unresolved is not None and report.unresolved.diagnostics.fences == ()
        )
        with pytest.raises(SessionStoreError, match="recovery_required"):
            api.fence_tool(active, fence(bind))
        report = api.inspect(SESSION)  # the failed retry invalidated the old inspection
        api.release_turn(report.revision, turn.turn_id)
        report = api.inspect(SESSION)
        api.reconcile(
            report.revision,
            HostResolution(
                session_id=SESSION,
                turn_id=turn.turn_id,
                actor="host",
                resolved_at_utc=T4,
                binding=bind,
            ),
        )
        report = api.inspect(SESSION)
        turn = intent(bind, report.revision, turn_id=OTHER_TURN, time=T4)
        active = api.begin_turn(report.revision, turn)
        invalid = settled(turn).model_copy(update={"checkpoint": "invalid container"})
        with pytest.raises(SessionStoreError):
            api.commit_turn(active, invalid)
        assert api.inspect(SESSION).local_fault
        with pytest.raises(SessionStoreError, match="recovery_required"):
            api.commit_turn(active, settled(turn))
    finally:
        api.close()


def test_metadata_wall_clock_does_not_order_revisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bind = binding(tmp_path)
    api = store(tmp_path)
    try:
        initial = api.create(bind).revision
        monkeypatch.setattr(
            "cairn.infra.sessions.store._now", lambda: "2019-12-31T23:59:59Z"
        )
        active = api.begin_turn(initial, intent(bind, initial))
        report = api.inspect(SESSION)
        assert report.revision == active
        assert report.status == "executing"
        assert report.checkpoint.seq == 0
    finally:
        api.close()
