import asyncio
import hashlib
from contextlib import ExitStack, suppress

from cairn.core.budget import RunBudget, RunBudgetExceeded
from cairn.core.context import ContextBudget, ContextBudgetExceeded, ContextBuilder
from cairn.core.events import Event, EventHandler
from cairn.core.llm import LLMClient
from cairn.core.permissions import PermissionHandler
from cairn.core.repository import RepositoryEvidence
from cairn.core.tasks import TaskResult, TaskSpec, TaskStatus
from cairn.infra.git.context import RepoContextProvider
from cairn.infra.observability.tracer import Tracer
from cairn.infra.sessions.models import Revision, SessionStoreError
from cairn.infra.sessions.task_models import (
    TaskDiagnostic,
    TaskRecord,
    TaskRepositoryObservation,
    TaskResultData,
)
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.agent import Agent, SessionAdmissionError
from cairn.runtime.assembly import build_agent
from cairn.runtime.durable_session import DurableSession
from cairn.runtime.lifecycle import settle_owned
from cairn.runtime.loop import run_turn
from cairn.runtime.model_executor import ModelExecutor


class CodingTaskRunner:
    """Run one mission through run_turn in a caller-owned Workspace.

    Stateless runs build a fresh Agent; durable runs require a fresh host binding.
    The caller owns the LLM, executor, tracer,
    permissions and directory. Cooperative cancellation covers execution;
    post-run inspection then records available facts. External cancellation
    always propagates after owned tasks settle, including repeated cancellation.
    """

    def __init__(
        self,
        *,
        workspace: Workspace,
        llm: LLMClient,
        budget: RunBudget,
        context_builder: ContextBuilder | None = None,
        context_budget: ContextBudget | None = None,
        permission_handler: PermissionHandler | None = None,
        event_handler: EventHandler | None = None,
        tracer: Tracer | None = None,
        model_executor: ModelExecutor | None = None,
        secret_env_keys: frozenset[str] = frozenset(),
    ) -> None:
        if context_builder is not None and context_budget is not None:
            raise ValueError("Provide context_builder or context_budget, not both")
        self.workspace = workspace
        self.llm = llm
        self.budget = budget
        self.context_builder = (
            context_builder
            if context_builder is not None
            else ContextBuilder(budget=context_budget)
        )
        self.permission_handler = permission_handler
        self.event_handler = event_handler
        self.tracer = tracer
        self.model_executor = model_executor
        self.secret_env_keys = secret_env_keys

    async def run(
        self,
        spec: TaskSpec,
        *,
        cancellation_event: asyncio.Event | None = None,
        durable_session: DurableSession | None = None,
    ) -> TaskResult:
        # Shield the owned lifecycle so repeated caller cancellation cannot
        # interrupt its child/process cleanup. This task is always observed.
        worker = asyncio.create_task(
            self._execute(spec, cancellation_event, durable_session)
        )
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError as primary:
            caller = asyncio.current_task()
            caller_cancelled = caller is not None and caller.cancelling() > 0
            await settle_owned(worker)
            try:
                result = worker.result()
                if result.error is not None:
                    primary.add_note(f"Task interrupted: {result.error}")
            except asyncio.CancelledError as exc:
                if not caller_cancelled:
                    raise exc
                for note in getattr(exc, "__notes__", ()):
                    primary.add_note(note)
            except Exception as exc:
                primary.add_note(f"Task cleanup failed: {type(exc).__name__}: {exc}")
            raise primary

    async def _turn(
        self,
        agent: Agent,
        prompt: str,
        cancellation_event: asyncio.Event | None,
        durable_session: DurableSession | None = None,
    ) -> str | None:
        execution = (
            run_turn(agent, prompt, budget=self.budget)
            if durable_session is None
            else run_turn(agent, prompt, budget=self.budget, session=durable_session)
        )
        if cancellation_event is None:
            return await execution

        turn = asyncio.create_task(execution)
        watcher = asyncio.create_task(cancellation_event.wait())
        primary: BaseException | None = None
        try:
            await asyncio.wait((turn, watcher), return_when=asyncio.FIRST_COMPLETED)
            # A completed turn wins a simultaneous cancellation signal.
            if turn.done():
                return turn.result()
            turn.cancel()
            try:
                await asyncio.shield(turn)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
            return None
        except BaseException as exc:
            primary = exc
            raise
        finally:
            for task in (turn, watcher):
                if not task.done() and not task.cancelling():
                    task.cancel()
            settled = asyncio.gather(turn, watcher, return_exceptions=True)
            try:
                outcomes = await asyncio.shield(settled)
            except asyncio.CancelledError as exc:
                outcomes = await asyncio.shield(settled)
                for outcome in outcomes:
                    if isinstance(outcome, Exception):
                        exc.add_note(f"Task cleanup failed: {outcome}")
                raise
            if primary is not None:
                for outcome in outcomes:
                    if isinstance(outcome, Exception) and outcome is not primary:
                        primary.add_note(f"Task cleanup failed: {outcome}")

    async def _execute(
        self,
        spec: TaskSpec,
        cancellation_event: asyncio.Event | None,
        durable_session: DurableSession | None = None,
    ) -> TaskResult:
        trace_id: str | None = None
        final_response: str | None = None
        error: str | None = None
        status = TaskStatus.COMPLETED
        task: TaskRecord | None = None
        diagnostic: TaskDiagnostic = "storage_failed"
        ownership = ExitStack()

        def forward_event(event: Event) -> None:
            nonlocal trace_id, final_response
            if event.type in ("trace_start", "trace_finish"):
                trace_id = event.data["trace_id"]
            elif event.type == "agent_finish":
                final_response = event.data["content"] or ""
            if self.event_handler is not None:
                self.event_handler(event)

        try:
            try:
                if cancellation_event is not None and cancellation_event.is_set():
                    status = TaskStatus.CANCELLED
                elif durable_session is not None:
                    # Revalidate even model_copy/model_construct inputs. Task
                    # attribution comes from the host, never prompt/model text.
                    spec = TaskSpec.model_validate(spec.model_dump())
                    self._validate_durable_agent(durable_session)
                    revision = durable_session.validate_task_binding(self.budget)
                    task = self._admit_task(durable_session, revision, spec)
                    ownership.enter_context(durable_session.task_execution(task))
                    agent = durable_session.agent
                    original_handler = agent.event_handler
                    agent.event_handler = forward_event
                    ownership.callback(
                        setattr, agent, "event_handler", original_handler
                    )
                    response = await self._turn(
                        agent, spec.prompt, cancellation_event, durable_session
                    )
                    if response is None:
                        status = TaskStatus.CANCELLED
                    else:
                        final_response = response
                else:
                    agent = build_agent(
                        workspace=self.workspace,
                        llm=self.llm,
                        permission_handler=self.permission_handler,
                        event_handler=forward_event,
                        tracer=self.tracer,
                        context_builder=self.context_builder,
                        model_executor=self.model_executor,
                        secret_env_keys=self.secret_env_keys,
                    )
                    response = await self._turn(agent, spec.prompt, cancellation_event)
                    if response is None:
                        status = TaskStatus.CANCELLED
                    else:
                        final_response = response
            except asyncio.CancelledError:
                diagnostic = "interrupted"
                if task is not None and durable_session is not None:
                    # The Turn may have checkpointed cancellation. Preserve that
                    # receipt separately; external cancellation skips inspection.
                    with suppress(Exception):
                        task = self._commit_task_turn(durable_session, task)
                raise
            except (RunBudgetExceeded, ContextBudgetExceeded) as exc:
                status = TaskStatus.BUDGET_EXHAUSTED
                error = (
                    f"{type(exc).__name__}: {exc}"
                    if durable_session is None
                    else "Task execution budget exhausted."
                )
            except Exception as exc:
                status = TaskStatus.RUNTIME_ERROR
                error = (
                    f"{type(exc).__name__}: {exc}"
                    if durable_session is None
                    else "Durable Task requires recovery."
                )

            inspect = durable_session is None or status is TaskStatus.CANCELLED
            if task is not None and durable_session is not None:
                try:
                    task = self._commit_task_turn(durable_session, task)
                    task = durable_session.store.start_task_inspection(task.revision)
                    inspect = True
                except Exception:
                    status = TaskStatus.RUNTIME_ERROR
                    error = "Durable Task requires recovery."
                    inspect = False

            repository = RepositoryEvidence(workspace_root=self.workspace.root)
            if inspect:
                try:
                    if task is not None and durable_session is not None:
                        durable_session.validate_task_inspection(
                            self.budget, durable_session.receipt
                        )
                    repository = await RepoContextProvider(
                        self.workspace
                    ).inspect_evidence(secret_env_keys=self.secret_env_keys)
                    if (
                        task is not None
                        and durable_session is not None
                        and repository.inspection_error is None
                    ):
                        durable_session.validate_task_inspection(
                            self.budget, durable_session.receipt
                        )
                except Exception as exc:
                    repository = RepositoryEvidence(
                        workspace_root=self.workspace.root,
                        inspection_error=(
                            f"{type(exc).__name__}: {exc}"
                            if durable_session is None
                            else "Repository inspection unavailable."
                        ),
                    )
                if repository.inspection_error is not None:
                    diagnostic = "inspection_failed"
                    if status is TaskStatus.COMPLETED:
                        status = TaskStatus.RUNTIME_ERROR
                    inspection_error = (
                        f"Repository inspection failed: {repository.inspection_error}"
                        if durable_session is None
                        else "Repository inspection failed; Task requires recovery."
                    )
                    error = (
                        f"{error}; {inspection_error}" if error else inspection_error
                    )
                elif task is not None and durable_session is not None:
                    try:
                        data = TaskResultData.model_validate(
                            {
                                "status": status.value,
                                "trace_id": trace_id,
                                "repository": TaskRepositoryObservation(
                                    is_git_repository=repository.is_git_repository,
                                    head_revision=repository.head_revision,
                                    dirty=repository.dirty,
                                ),
                            }
                        )
                        task = durable_session.store.record_task_result(
                            task.revision, data
                        )
                    except Exception:
                        status = TaskStatus.RUNTIME_ERROR
                        error = "Durable Task requires recovery."
            return TaskResult(
                task_id=spec.task_id,
                status=status,
                final_response=final_response,
                trace_id=trace_id,
                error=error,
                repository=repository,
            )
        except asyncio.CancelledError:
            diagnostic = "interrupted"
            raise
        finally:
            try:
                if task is not None and durable_session is not None:
                    self._require_task_recovery(durable_session, task, diagnostic)
            finally:
                ownership.close()

    def _validate_durable_agent(self, session: DurableSession) -> None:
        agent = session.agent
        provider = agent.repo_context_provider
        if (
            agent.llm is not self.llm
            or agent.permission_handler is not self.permission_handler
            or agent.event_handler is not self.event_handler
            or agent.tracer is not self.tracer
            or agent.context_builder.budget != self.context_builder.budget
            or (
                self.model_executor is not None
                and agent.model_executor is not self.model_executor
            )
            or provider is None
            or provider.workspace.root != self.workspace.root
            or provider.secret_env_keys != self.secret_env_keys
        ):
            raise SessionAdmissionError("Task runtime configuration mismatch.")

    def _admit_task(
        self, session: DurableSession, revision: Revision, spec: TaskSpec
    ) -> TaskRecord:
        digest = hashlib.sha256(spec.prompt.encode("utf-8")).hexdigest()
        try:
            return session.store.create_task(
                revision, task_id=spec.task_id, input_digest=digest
            )
        except (SessionStoreError, asyncio.CancelledError) as exc:
            if (
                isinstance(exc, asyncio.CancelledError)
                or exc.code == "commit_uncertain"
            ):
                # Admission might be stored despite a missing acknowledgement.
                # Observe it, never repeat INSERT or begin a Turn.
                try:
                    task = session.store.inspect_task(
                        str(session.session_id), spec.task_id
                    ).task
                    if task.base_revision == revision and task.input_digest == digest:
                        self._require_task_recovery(
                            session,
                            task,
                            "interrupted"
                            if isinstance(exc, asyncio.CancelledError)
                            else "storage_failed",
                        )
                except Exception:
                    pass
            raise

    def _commit_task_turn(
        self, session: DurableSession, task: TaskRecord
    ) -> TaskRecord:
        report = session.inspect()
        receipt = session.receipt
        last = session.last_turn
        if (
            report.status != "clean"
            or report.local_fault
            or receipt != report.receipt
            or last is None
            or receipt.turn_id != str(last.turn_id)
            or receipt.revision.head_seq != task.base_revision.head_seq + 1
        ):
            raise SessionStoreError("task_checkpoint_mismatch")
        # The Store verifies the original receipt, Task ID, digest, sequence and
        # exact Task CAS in one transaction. A Turn commit is not a Task result.
        return session.store.commit_task_turn(task.revision, receipt)

    def _require_task_recovery(
        self, session: DurableSession, task: TaskRecord, diagnostic: TaskDiagnostic
    ) -> None:
        try:
            # Inspect after every failed/unknown write before selecting the CAS.
            # A result already recorded must never be overwritten or repeated.
            current = session.store.inspect_task(
                str(session.session_id), task.revision.task_id
            ).task
            if (
                current.base_revision == task.base_revision
                and current.input_digest == task.input_digest
                and current.phase != "result_recorded"
                and not current.recovery_required
            ):
                session.store.require_task_recovery(current.revision, diagnostic)
        except Exception:
            # Unreadable/unwritable stores still retain incomplete facts and the
            # existing fault gate. Arbitrary diagnostics never enter the ledger.
            pass
