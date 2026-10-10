import asyncio

from cairn.assembly import build_agent
from cairn.core.agent import Agent
from cairn.core.budget import RunBudget, RunBudgetExceeded
from cairn.core.context import ContextBudget, ContextBudgetExceeded, ContextBuilder
from cairn.core.events import Event, EventHandler
from cairn.core.loop import run_turn
from cairn.core.permissions import PermissionHandler
from cairn.llm.base import LLMClient
from cairn.llm.model_executor import ModelExecutor
from cairn.observability.tracer import Tracer
from cairn.repository import RepoContextProvider, RepositoryEvidence
from cairn.tasks.models import TaskResult, TaskSpec, TaskStatus
from cairn.workspace.workspace import Workspace


class CodingTaskRunner:
    """Run one mission through run_turn in a caller-owned Workspace.

    Each run builds a fresh Agent. The caller owns the LLM, executor, tracer,
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
        self, spec: TaskSpec, *, cancellation_event: asyncio.Event | None = None
    ) -> TaskResult:
        # Shield the owned lifecycle so repeated caller cancellation cannot
        # interrupt its child/process cleanup. This task is always observed.
        worker = asyncio.create_task(self._execute(spec, cancellation_event))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError as primary:
            caller = asyncio.current_task()
            caller_cancelled = caller is not None and caller.cancelling() > 0
            if not worker.done():
                worker.cancel()
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
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
        self, agent: Agent, prompt: str, cancellation_event: asyncio.Event | None
    ) -> str | None:
        if cancellation_event is None:
            return await run_turn(agent, prompt, budget=self.budget)

        turn = asyncio.create_task(run_turn(agent, prompt, budget=self.budget))
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
        self, spec: TaskSpec, cancellation_event: asyncio.Event | None
    ) -> TaskResult:
        trace_id: str | None = None
        final_response: str | None = None
        error: str | None = None
        status = TaskStatus.COMPLETED

        def forward_event(event: Event) -> None:
            nonlocal trace_id, final_response
            if event.type in ("trace_start", "trace_finish"):
                trace_id = event.data["trace_id"]
            elif event.type == "agent_finish":
                final_response = event.data["content"] or ""
            if self.event_handler is not None:
                self.event_handler(event)

        try:
            if cancellation_event is not None and cancellation_event.is_set():
                status = TaskStatus.CANCELLED
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
                    repository_secret_env_keys=self.secret_env_keys,
                )
                response = await self._turn(agent, spec.prompt, cancellation_event)
                if response is None:
                    status = TaskStatus.CANCELLED
                else:
                    final_response = response
        except (RunBudgetExceeded, ContextBudgetExceeded) as exc:
            status = TaskStatus.BUDGET_EXHAUSTED
            error = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            status = TaskStatus.RUNTIME_ERROR
            error = f"{type(exc).__name__}: {exc}"

        try:
            repository = await RepoContextProvider(self.workspace).inspect_evidence(
                secret_env_keys=self.secret_env_keys
            )
        except Exception as exc:
            repository = RepositoryEvidence(
                workspace_root=self.workspace.root,
                inspection_error=f"{type(exc).__name__}: {exc}",
            )
        if repository.inspection_error is not None:
            if status is TaskStatus.COMPLETED:
                status = TaskStatus.RUNTIME_ERROR
            inspection_error = (
                f"Repository inspection failed: {repository.inspection_error}"
            )
            error = f"{error}; {inspection_error}" if error else inspection_error
        return TaskResult(
            task_id=spec.task_id,
            status=status,
            final_response=final_response,
            trace_id=trace_id,
            error=error,
            repository=repository,
        )
