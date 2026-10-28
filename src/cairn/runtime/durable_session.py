"""Opt-in storage at the existing Session/Agent turn boundary; never a replay loop."""

import hashlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn
from uuid import UUID

from cairn.core.budget import RunBudget
from cairn.core.events import Event
from cairn.core.models import Message, ToolCall
from cairn.core.permissions import PermissionCapability, SessionPermissionHandler
from cairn.infra.sessions.codec import (
    decode_record,
    encode_checkpoint,
    encode_record,
    encode_storage_checkpoint,
    restore_messages,
    snapshot_messages,
)
from cairn.infra.sessions.models import (
    MAX_MESSAGE_BYTES,
    MAX_MESSAGES,
    CheckpointPayload,
    CheckpointReceipt,
    HostResolution,
    MessageRange,
    Outcome,
    Reason,
    RecoveryReport,
    Revision,
    SessionBinding,
    SessionStoreError,
    SettledTurn,
    StoredMessage,
    ToolFence,
    TraceSummary,
    TurnIntent,
    same_workspace,
)
from cairn.infra.sessions.store import SessionStore
from cairn.infra.sessions.task_models import TaskRecord, TaskRecoveryReport
from cairn.runtime.agent import Agent, SessionAdmissionError
from cairn.runtime.session import Session, SessionState, TurnOutcome
from cairn.runtime.turn_trace import TurnTraceSummary


class SessionDurabilityError(RuntimeError):
    """Execution has no durable acknowledgement; inspect before new admission."""

    def __init__(self) -> None:
        super().__init__("Durable session requires recovery.")


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class DurableSession(Session):
    """One exact Agent and writer Store. Factories bind before attachment I/O.

    The trusted host supplies observed/approved bindings, never model input.
    Git bindings require a synchronous observation callback for start/end facts;
    reopen does not call it. No stored binding supplies credentials or grants.
    """

    def __init__(
        self,
        store: SessionStore,
        *,
        agent: Agent,
        observe_binding: Callable[[], SessionBinding] | None = None,
    ) -> None:
        self._store = store
        self._observe_binding = observe_binding
        self._suspended = True
        self._recovery = False
        self._reason: Reason = "storage_failed"
        self._report: RecoveryReport | None = None
        self._intent: TurnIntent | None = None
        self._owned = False
        self._cleanup_settled = False
        self._ordinal = 0
        self._committed: CheckpointReceipt | None = None
        self._expected_receipt: CheckpointReceipt | None = None
        self._task: TaskRecord | None = None
        self._task_started = False
        super().__init__(agent)

    @classmethod
    def create(
        cls,
        store: SessionStore,
        *,
        agent: Agent,
        binding: SessionBinding,
        observe_binding: Callable[[], SessionBinding] | None = None,
    ) -> "DurableSession":
        session = cls(store, agent=agent, observe_binding=observe_binding)
        try:
            session._attach(binding)
            session._receipt = store.create(session._binding)
            session._revision = session._receipt.revision
            session._suspended = False
            return session
        except BaseException:
            session._closed = True
            raise

    @classmethod
    def reopen(
        cls,
        store: SessionStore,
        session_id: str,
        *,
        agent: Agent,
        binding: SessionBinding,
        observe_binding: Callable[[], SessionBinding] | None = None,
    ) -> "DurableSession":
        session = cls(store, agent=agent, observe_binding=observe_binding)
        try:
            session._attach(binding)
            if session_id != str(session.session_id):
                raise SessionAdmissionError("Session identity mismatch.")
            report = store.inspect(session_id)
            if not same_workspace(report.binding, session._binding):
                raise SessionAdmissionError("Workspace identity mismatch.")
            session._report = report
            session._revision = report.revision
            session._receipt = report.receipt
            # status already reports the store-wide latch; see RecoveryReport.
            session._recovery = report.status != "clean"
            agent.state.messages = restore_messages(report.checkpoint)
            return session
        except BaseException:
            session._closed = True
            raise

    def _attach(self, binding: SessionBinding) -> None:
        if self._store.mode != "writer" or self.agent.state.messages:
            raise SessionAdmissionError(
                "Durable attachment needs a fresh Agent/writer."
            )
        try:
            binding = decode_record(
                SessionBinding,
                encode_record(binding, sensitive_values=self._store.sensitive_values),
            )
            self._validate_binding(binding)
        except SessionStoreError:
            raise SessionAdmissionError("Invalid durable binding.") from None
        self._session_id = UUID(binding.session_id)
        self._binding = binding
        self._validate_permissions()
        self._pin_runtime()

    def _validate_permissions(self) -> None:
        """Refuse to attach with authority carried over from an earlier session.

        ``WORKSPACE_TRUST`` is not carried over: it is confirmed again at every
        start, before any turn, so it is the baseline this session begins with
        rather than a leftover grant.
        """
        handler = self.agent.permission_handler
        if not isinstance(handler, SessionPermissionHandler):
            return
        carried = handler.grants - {PermissionCapability.WORKSPACE_TRUST}
        if carried:
            raise SessionAdmissionError("Durable attachment needs fresh permissions.")

    def _pin_runtime(self) -> None:
        self._runtime = self._live_runtime()

    def _live_runtime(self) -> tuple[object, ...]:
        agent = self.agent
        manager = agent.model_executor.manager
        return (
            agent.llm,
            agent.context_builder,
            agent.model_executor,
            agent.permission_handler,
            agent.repo_context_provider,
            agent.memory_reader,
            agent.provider_name,
            agent.system_prompt,
            agent.context_builder.budget,
            manager.config if manager is not None else None,
            manager.current_model() if manager is not None else None,
        )

    def _check_runtime(self) -> None:
        current = self._live_runtime()
        # Collaborator equality does not grant object ownership. The first six
        # entries are exact objects; the rest are frozen configuration facts.
        if (
            any(a is not b for a, b in zip(self._runtime[:6], current[:6], strict=True))
            or self._runtime[6:] != current[6:]
        ):
            raise SessionAdmissionError("Runtime binding requires reconciliation.")

    def _validate_binding(self, binding: SessionBinding) -> None:
        try:
            workspace = binding.workspace
            identities = [workspace.root]
            if workspace.git is not None:
                if self._observe_binding is None:
                    raise SessionAdmissionError(
                        "Git binding requires host observation."
                    )
                identities.extend(
                    (
                        workspace.git.repository,
                        workspace.git.common,
                        workspace.git.admin,
                    )
                )
            for identity in identities:
                path = Path(identity.path)
                info = path.stat()
                if (
                    str(path.resolve(strict=True)) != identity.path
                    or not path.is_dir()
                    or (info.st_dev, info.st_ino) != (identity.device, identity.inode)
                ):
                    raise SessionAdmissionError("Workspace identity mismatch.")
            agent = self.agent
            if (
                agent.repo_context_provider is not None
                and str(agent.repo_context_provider.workspace.root)
                != workspace.root.path
            ):
                raise SessionAdmissionError("Agent Workspace mismatch.")
            if (
                agent.memory_reader is not None
                and str(agent.memory_reader.resolver.workspace.root)
                != workspace.root.path
            ):
                raise SessionAdmissionError("Agent Workspace mismatch.")
            provider = binding.provider
            manager = agent.model_executor.manager
            if provider is None:
                if agent.provider_name is not None or manager is not None:
                    raise SessionAdmissionError("Provider binding is required.")
            elif (
                provider.provider != agent.provider_name
                or provider.system_prompt_digest
                != hashlib.sha256(agent.system_prompt.encode("utf-8")).hexdigest()
                or provider.context_budget != agent.context_builder.budget.max_tokens
                or provider.output_reserve
                != agent.context_builder.budget.response_tokens
                or manager is None
                or provider.model_group != manager.current_model().name
                or provider.model_ids != manager.current_model().model_ids
            ):
                raise SessionAdmissionError("Provider binding mismatch.")
        except (OSError, ValueError, TypeError):
            raise SessionAdmissionError("Binding unavailable.") from None

    def _observe(self) -> SessionBinding:
        try:
            binding = (
                self._binding
                if self._observe_binding is None
                else self._observe_binding()
            )
            binding = decode_record(
                SessionBinding,
                encode_record(binding, sensitive_values=self._store.sensitive_values),
            )
            self._validate_binding(binding)
            if not same_workspace(self._binding, binding):
                raise SessionAdmissionError("Workspace identity mismatch.")
            return binding
        except Exception:
            raise SessionAdmissionError("Binding unavailable.") from None

    @property
    def state(self) -> SessionState:
        if self.active_turn is not None or self.close_requested:
            return super().state
        if self._recovery:
            return SessionState.RECOVERY_REQUIRED
        return SessionState.SUSPENDED if self._suspended else SessionState.READY

    def trace_correlation(self) -> dict[str, str]:
        """Session, turn and (when bound) task ids for tracing one turn."""
        correlation = super().trace_correlation()
        if self._task is not None:
            correlation["task_id"] = self._task.revision.task_id
        return correlation

    @property
    def revision(self) -> Revision:
        return self._revision

    @property
    def receipt(self) -> CheckpointReceipt:
        """Last observed receipt, including exact inspection after uncertain COMMIT."""
        return self._receipt

    @property
    def store(self) -> SessionStore:
        """The exact host-supplied Store, without opening or upgrading it."""
        return self._store

    def validate_task_binding(self, budget: RunBudget) -> Revision:
        """Validate a fresh, ready host binding before Task admission/effects."""
        if (
            self.state is not SessionState.READY
            or self.last_turn is not None
            or self._task is not None
            or self.agent.permission_handler is None
        ):
            raise SessionAdmissionError("A fresh ready Task binding is required.")
        self._validate_permissions()
        report = self._check_binding(budget)
        if snapshot_messages(self.agent.state.messages) != report.checkpoint.messages:
            raise SessionAdmissionError("Conversation differs from committed history.")
        return report.revision

    def validate_task_inspection(
        self, budget: RunBudget, receipt: CheckpointReceipt
    ) -> None:
        """Check current host observations around an owned post-turn inspection."""
        if self._task is None or not self._task_started:
            raise SessionAdmissionError("An owned Task Turn is required.")
        report = self._check_binding(budget)
        if receipt != self._receipt or receipt != report.receipt:
            raise SessionAdmissionError("Exact Task checkpoint is required.")

    @contextmanager
    def task_execution(self, task: TaskRecord) -> Iterator[None]:
        """Attribute one intended Turn to an exact admitted, host-owned Task.

        Keep both the Session and writer bound through post-turn inspection and
        cleanup. Closing the Session requests shutdown without releasing work.
        """
        if self._task is not None or task.base_revision != self._revision:
            raise SessionAdmissionError("Exact Task admission is required.")
        current = self._store.inspect_task(
            task.revision.session_id, task.revision.task_id
        )
        if current.task != task or task.revision.session_id != str(self.session_id):
            raise SessionAdmissionError("Exact Task admission is required.")
        try:
            with self._store.task_execution(task.revision):
                self._task = task
                self._task_started = False
                try:
                    yield
                finally:
                    self._task = None
        finally:
            if self.close_requested:
                self.close()

    @contextmanager
    def task_reconciliation(
        self, report: TaskRecoveryReport, *, budget: RunBudget
    ) -> Iterator[None]:
        """Own host-only recovery through inspection and cancellation cleanup.

        Requires a separately reconciled, fresh Session. It admits no Turn and
        grants no tool/provider permission; Task decisions belong to the host.
        """
        if (
            self.state is not SessionState.READY
            or self.active_turn is not None
            or self._task is not None
            or self._owned
            or report.task.revision.session_id != str(self.session_id)
        ):
            raise SessionAdmissionError("Exact idle Task recovery is required.")
        self._validate_permissions()
        current = self._check_binding(budget)
        if (
            current.revision != report.session_revision
            or current.receipt != report.session_checkpoint
            or snapshot_messages(self.agent.state.messages)
            != current.checkpoint.messages
        ):
            raise SessionAdmissionError("Exact Task recovery facts are required.")
        try:
            with self._store.task_reconciliation(report):
                self._task = report.task
                # The existing admission gate must refuse to run this old Task.
                self._task_started = True
                try:
                    yield
                finally:
                    self._task = None
        finally:
            if self.close_requested:
                self.close()

    @property
    def recovery_report(self) -> RecoveryReport:
        return self.inspect()

    def inspect(self) -> RecoveryReport:
        # An idle retry may release ownership only after the loop proved cleanup
        # complete. Inspect/reconcile during execution can never provide that proof.
        if self._owned and self._cleanup_settled and self.active_turn is None:
            self._release_failed_turn()
        report = self._store.inspect(str(self.session_id))
        self._report = report
        return report

    def reconcile(
        self, report: RecoveryReport, *, host_resolution: HostResolution
    ) -> None:
        if (
            self.active_turn is not None
            or self.close_requested
            or self._task is not None
        ):
            raise SessionAdmissionError("Session is executing or closed.")
        current = self.inspect()
        if report != current or self._owned:
            raise SessionAdmissionError("Exact idle recovery report required.")
        binding = host_resolution.binding
        self._validate_binding(binding)
        self._validate_permissions()
        self._revision = self._store.reconcile(report.revision, host_resolution)
        self._binding = binding
        self._receipt = report.receipt
        # Explicit host abandonment is the only operation that discards an
        # uncommitted live tail. It never manufactures results for crashed calls.
        self.agent.state.messages = restore_messages(report.checkpoint)
        self._report = self._store.inspect(str(self.session_id))
        self._pin_runtime()
        self._suspended = self._recovery = False

    def _check_binding(self, budget: RunBudget) -> RecoveryReport:
        report = self._store.inspect(str(self.session_id))
        if (
            report.revision != self._revision
            or report.status != "clean"
            or report.binding != self._binding
        ):
            raise SessionAdmissionError("Durable Session requires reconciliation.")
        self._check_runtime()
        observed = self._observe()
        if (
            observed.model_copy(
                update={"observed_at_utc": self._binding.observed_at_utc}
            )
            != self._binding
        ):
            raise SessionAdmissionError("Host binding requires reconciliation.")
        if (
            self._binding.provider is not None
            and budget.max_steps != self._binding.provider.max_steps
        ):
            raise SessionAdmissionError("Run budget binding mismatch.")
        return report

    # Turn admission stages and failure ownership
    # ------------------------------------------------------------------------
    #
    # ┌────────────────────────┐
    # │     _admit_durable     │
    # └────────────────────────┘
    #              │
    #              v
    # ┌────────────────────────┐
    # │ super()._admit (guard) │
    # └────────────────────────┘
    #              │
    #              v
    # ┌────────────────────────┐
    # │  _check_binding_stage  │  <- fail -> suspended
    # └────────────────────────┘
    #              │
    #              v
    # ┌────────────────────────┐
    # │ _validate_input_stage  │  <- fail -> guard released
    # └────────────────────────┘
    #              │
    #              v
    # ┌────────────────────────┐
    # │   _begin_turn_stage    │  <- fail -> recovery
    # └────────────────────────┘
    #              │
    #              v
    # ┌────────────────────────┐
    # │       executing        │
    # └────────────────────────┘
    #
    # Ownership rules:
    #   * The in-memory Turn guard is released on every path, including
    #     cancellation.
    #   * A cancelled caller keeps its own error; an ordinary exception is
    #     replaced by the fixed durability error with no cause attached,
    #     because store text can carry host paths and credentials.
    #   * ``_owned`` tracks this process's execution ownership only. A stored
    #     record never acquires it, and release_turn additionally requires the
    #     store to be in the faulted state.
    # ------------------------------------------------------------------------

    def _admit_durable(self, agent: Agent, user_input: str, budget: RunBudget) -> None:
        """Reserve durable admission, then begin the Turn.

        Three stages can fail, and each abandons a different amount of state:
        a binding that fails suspends the session, a rejected input only releases
        the Turn guard, and a failed ``begin_turn`` requires host recovery. Each
        stage names its own failure policy instead of setting flags inline.
        """
        if self.state is not SessionState.READY:
            raise SessionAdmissionError("Durable Session is not ready.")
        # Reserve the same Session guard synchronously, before the first effect.
        super()._admit(agent)
        report = self._check_binding_stage(budget, user_input)
        history = self._validate_input_stage(agent, user_input, report)
        self._begin_turn_stage(user_input, history)

    def _check_binding_stage(
        self, budget: RunBudget, user_input: str
    ) -> RecoveryReport:
        """Verify the committed binding and any Task attribution.

        A failure here suspends the session: the store may have moved on, so the
        host must reconcile before this binding can be trusted again.
        """
        try:
            report = self._check_binding(budget)
            if self._task is not None:
                task = self._task
                current = self._store.inspect_task(
                    str(self.session_id), task.revision.task_id
                )
                if (
                    self._task_started
                    or current.task != task
                    or task.base_revision != self._revision
                    or task.input_digest
                    != hashlib.sha256(user_input.encode("utf-8")).hexdigest()
                ):
                    raise SessionAdmissionError("Task Turn attribution mismatch.")
                self._task_started = True
            return report
        except BaseException as exc:
            self._abandon_admission(
                exc,
                suspended=True,
                message="Durable binding requires reconciliation.",
            )

    def _validate_input_stage(
        self, agent: Agent, user_input: str, report: RecoveryReport
    ) -> tuple[StoredMessage, ...]:
        """Reject an input or history the store could not accept.

        Nothing was written yet, so only the Turn guard is released. A history
        that differs from the committed checkpoint additionally suspends the
        session, because the live conversation is no longer the stored one.
        """
        try:
            if type(user_input) is not str or len(agent.state.messages) >= MAX_MESSAGES:
                raise SessionAdmissionError("Durable input rejected.")
            history = snapshot_messages(agent.state.messages)
            if history != report.checkpoint.messages:
                self._suspended = True
                raise SessionAdmissionError(
                    "Conversation differs from committed history."
                )
            # Validate user text/known credentials before an intent (which stores
            # only its digest). No provisional response is serialized here.
            encode_record(
                snapshot_messages([Message(role="user", content=user_input)])[0],
                limit=MAX_MESSAGE_BYTES,
                sensitive_values=self._store.sensitive_values,
            )
            now = _now()
            # Reserve the full input/history envelope, not just one message.
            # This is a validated interrupted tail, never a provisional response
            # or a write. Output still must pass the final checkpoint limits.
            encode_checkpoint(
                CheckpointPayload(
                    session_id=str(self.session_id),
                    turn_id=str(self.active_turn.turn_id) if self.active_turn else None,
                    seq=self._revision.head_seq + 1,
                    binding=self._binding.model_copy(update={"observed_at_utc": now}),
                    messages=(
                        *history,
                        *snapshot_messages([Message(role="user", content=user_input)]),
                    ),
                    message_range=MessageRange(
                        start=len(history), end=len(history) + 1
                    ),
                    outcome="cancelled",
                    started_at_utc=now,
                    settled_at_utc=now,
                ),
                sensitive_values=self._store.sensitive_values,
            )
            return history
        except BaseException as exc:
            self._abandon_admission(
                exc, suspended=False, message="Durable admission rejected."
            )

    def _begin_turn_stage(
        self, user_input: str, history: tuple[StoredMessage, ...]
    ) -> None:
        """Record the Turn intent and let the store begin it.

        A failure here requires host recovery: the store may already own the
        Turn, so the session must never continue as if admission succeeded.
        """
        assert self.active_turn is not None
        self._ordinal = 0
        self._reason = "storage_failed"
        self._committed = self._expected_receipt = None
        self._cleanup_settled = False
        try:
            self._intent = TurnIntent(
                session_id=str(self.session_id),
                turn_id=str(self.active_turn.turn_id),
                base_seq=self._revision.head_seq,
                message_start=len(history),
                input_digest=hashlib.sha256(user_input.encode("utf-8")).hexdigest(),
                started_at_utc=_now(),
                binding=self._binding,
                task_id=self._task.revision.task_id if self._task is not None else None,
            )
            self._revision = self._store.begin_turn(self._revision, self._intent)
            self._owned = True
        except BaseException as exc:
            self._abandon_admission(exc, began_turn=True)

    def _abandon_admission(
        self,
        exc: BaseException,
        *,
        suspended: bool = False,
        began_turn: bool = False,
        message: str = "",
    ) -> NoReturn:
        """Release the Turn guard, then raise this stage's admission failure.

        Always raises, so callers end their ``except`` with this call. The Turn
        guard is always released: admission is synchronous and no model or tool
        work was dispatched. ``_turn`` must be cleared before the raise, because
        ``state`` reports the active Turn first and would otherwise hide the
        recovery or suspension recorded here.

        A cancelled caller keeps its own error and traceback. An ordinary
        exception becomes the stage's fixed ``message`` with no cause attached,
        since store text can carry host paths and credentials.
        """
        self._turn = None
        if suspended:
            self._suspended = True
        if not began_turn:
            if isinstance(exc, Exception):
                raise SessionAdmissionError(message) from None
            raise exc

        # A host interruption can lose begin_turn's return after it acquired
        # local ownership. Admission is synchronous: no model/tool work was
        # dispatched, so cleanup is already settled. Keep the unfinished
        # intent, mark recovery and release only this exact local ownership.
        self._recovery = True
        self._cleanup_settled = True
        if self._intent is not None:
            self._owned = self._store.owns_turn(
                str(self.session_id), self._intent.turn_id
            )
        if self._owned:
            try:
                self._release_failed_turn()
            except Exception:
                exc.add_note("Durable session requires recovery.")
        replacement = self._durability_error(exc)
        if replacement is None:
            raise
        raise replacement from None

    def _before_execute(self, call: ToolCall, assistant_index: int) -> None:
        assert self._intent is not None
        try:
            # Validate arguments and known secrets without persisting them.
            encode_record(
                snapshot_messages([Message(role="assistant", tool_calls=[call])])[0],
                limit=MAX_MESSAGE_BYTES,
                sensitive_values=self._store.sensitive_values,
            )
            self._ordinal += 1
            self._revision = self._store.fence_tool(
                self._revision,
                ToolFence(
                    session_id=str(self.session_id),
                    turn_id=self._intent.turn_id,
                    ordinal=self._ordinal,
                    assistant_message_index=assistant_index,
                    call_id=call.id,
                    tool_name=call.name,
                    observed_at_utc=_now(),
                ),
            )
        except BaseException as exc:
            self._recovery = True
            _replacement = self._durability_error(exc)
            if _replacement is None:
                raise
            raise _replacement from None

    def _durability_error(self, exc: BaseException) -> BaseException | None:
        """The error to raise after durable state could not be advanced.

        Returns ``None`` when the caller must re-raise the original exception
        unchanged, which keeps a cancelled turn's primary error and traceback.
        An ordinary exception is replaced by the fixed durability error with no
        cause attached: store text could carry host paths or credentials.
        """
        if isinstance(exc, Exception):
            return SessionDurabilityError()
        return None

    # Commit, settle and release
    # ------------------------------------------------------------------------
    #
    # ┌──────────────────────┐
    # │   _commit(outcome)   │  # summary is None -> recovery
    # └──────────────────────┘
    #             │
    #             v
    # ┌──────────────────────┐
    # │   build checkpoint   │  # encode + predict receipt
    # └──────────────────────┘
    #             │
    #             v
    # ┌──────────────────────┐
    # │  store.commit_turn   │  # CAS on sessions.version
    # └──────────────────────┘
    #             │
    #             v
    # ┌──────────────────────┐
    # │ receipt == expected? │  # no -> durability error
    # └──────────────────────┘
    #             │
    #             v
    # ┌──────────────────────┐
    # │     _settle(...)     │  # notify, cleanup_settled
    # └──────────────────────┘
    #             │
    #             v
    # ┌──────────────────────┐
    # │   release or close   │  # finally: _finish_turn
    # └──────────────────────┘
    #
    # Only run_turn calls ``_settle``, after execution, rollback and trace
    # close. It keeps the guard until both notifications and any faulted
    # release finish, and ``_finish_turn`` runs in a finally block so the
    # guard is never left installed.
    # ------------------------------------------------------------------------

    def _commit(self, outcome: TurnOutcome, summary: TurnTraceSummary | None) -> None:
        if summary is None:
            self._recovery = True
            self._reason = "finalization_failed"
        if self._recovery:
            raise SessionDurabilityError()
        assert self._intent is not None
        try:
            self._check_runtime()
            binding = self._observe()
            now = _now()
            binding = binding.model_copy(update={"observed_at_utc": now})
            value: Outcome = outcome.value
            assert summary is not None
            checkpoint = CheckpointPayload(
                session_id=str(self.session_id),
                turn_id=self._intent.turn_id,
                seq=self._revision.head_seq + 1,
                binding=binding,
                messages=snapshot_messages(self.agent.state.messages),
                message_range=MessageRange(
                    start=self._intent.message_start, end=len(self.agent.state.messages)
                ),
                outcome=value,
                started_at_utc=self._intent.started_at_utc,
                settled_at_utc=now,
                trace=TraceSummary(
                    trace_id=summary.trace_id,
                    persisted=summary.persisted,
                    input_tokens=summary.input_tokens,
                    output_tokens=summary.output_tokens,
                    error_code=summary.error_code,
                ),
            )
            _, digest = encode_storage_checkpoint(
                checkpoint,
                self._receipt,
                format_version=2 if self._store.schema_version >= 3 else 1,
                sensitive_values=self._store.sensitive_values,
            )
            self._expected_receipt = CheckpointReceipt(
                revision=Revision(
                    session_id=str(self.session_id),
                    version=self._revision.version + 1,
                    head_seq=checkpoint.seq,
                ),
                turn_id=self._intent.turn_id,
                sha256=digest,
                outcome=value,
            )
            receipt = self._store.commit_turn(
                self._revision, SettledTurn(checkpoint=checkpoint)
            )
            self._owned = False  # Store released only after its successful commit.
            if receipt != self._expected_receipt:
                raise SessionDurabilityError()
            self._committed = self._receipt = receipt
            self._revision = receipt.revision
            self._binding = binding
        except SessionAdmissionError:
            self._reason = "binding_unavailable"
            self._recovery = True
            raise SessionDurabilityError() from None
        except BaseException as exc:
            self._recovery = True
            _replacement = self._durability_error(exc)
            if _replacement is None:
                raise
            raise _replacement from None

    def _release_failed_turn(self) -> None:
        report = self._store.inspect(str(self.session_id))
        if not report.local_fault:
            self._store.require_recovery(report.revision, self._reason)
            report = self._store.inspect(str(self.session_id))
        self._revision = report.revision
        self._report = report
        if (
            self._expected_receipt is not None
            and report.receipt == self._expected_receipt
        ):
            self._receipt = report.receipt  # Observation only; gate stays closed.
        if self._owned:
            assert self._intent is not None and self._cleanup_settled
            self._store.release_turn(report.revision, self._intent.turn_id)
            self._owned = False

    def _settle(self, outcome: TurnOutcome, trace_finalization_failed: bool) -> None:
        try:
            self._notify_settled(outcome, trace_finalization_failed)
            if self._committed is not None:
                try:
                    self._notify_lifecycle(
                        Event(
                            type="turn_committed",
                            data={
                                "checkpoint_seq": self._committed.revision.head_seq,
                                "revision": self._committed.revision.version,
                                "outcome": self._committed.outcome,
                            },
                        )
                    )
                except BaseException:
                    assert self._last_turn is not None
                    self._last_turn = replace(
                        self._last_turn, commit_delivery_failed=True
                    )
            # Only run_turn calls this, after execution, rollback and trace close.
            # Keep its guard until BOTH notifications and faulted release finish.
            self._cleanup_settled = True
            if self._recovery:
                self._release_failed_turn()
            if self.close_requested and self._task is None:
                self._store.close()
        except Exception:
            self._recovery = True
            raise SessionDurabilityError() from None
        finally:
            self._finish_turn()

    @property
    def recovery_notice(self) -> str | None:
        report = self._report
        if report is None or (not report.checkpoint.messages and not report.attempts):
            return None
        abandoned = [
            a.intent.turn_id for a in report.attempts if a.state == "abandoned"
        ]
        notice = "Restored tool observations are historical; inspect current state before new work."
        if abandoned:
            notice += (
                f" {len(abandoned)} abandoned attempts were manually reconciled; effects may exist."
                " Do not replay their previous prompts or calls. Recent Turn IDs: "
                + ", ".join(abandoned[-8:])
            )
        return notice

    def close(self) -> None:
        super().close()
        if self.active_turn is None and self._task is None:
            if self._owned and self._cleanup_settled:
                self._release_failed_turn()
            self._store.close()
