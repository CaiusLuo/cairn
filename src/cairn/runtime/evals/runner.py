import asyncio
import math
from collections.abc import Callable, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import TYPE_CHECKING, Any, ClassVar

from cairn.core.budget import RunBudget, RunBudgetExceeded
from cairn.core.context import (
    ContextBudget,
    ContextBudgetExceeded,
    ContextBuilder,
    ContextRequest,
    TokenCounter,
)
from cairn.core.events import Event, EventHandler
from cairn.core.llm import LLMClient
from cairn.infra.observability.tracer import Tracer
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.agent import Agent
from cairn.runtime.assembly import build_agent
from cairn.runtime.evals.checks import (
    FileContainsCheck,
    FileContentEqualsCheck,
    FileExistsCheck,
    FileNotContainsCheck,
)
from cairn.runtime.evals.models import (
    CheckResult,
    EvalCase,
    EvalCheck,
    EvalFailureCategory,
    EvalMetrics,
    EvalResult,
    EvalStatus,
)
from cairn.runtime.loop import run_turn

if TYPE_CHECKING:
    from cairn.runtime.memory.retrieval import MemoryReader


def _error_message(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def _check_target_path(check: EvalCheck, workspace: Workspace) -> str | None:
    if not isinstance(
        check,
        (
            FileExistsCheck,
            FileContentEqualsCheck,
            FileContainsCheck,
            FileNotContainsCheck,
        ),
    ):
        return None
    try:
        return workspace.resolve_path(check.path).relative_to(workspace.root).as_posix()
    except (ValueError, OSError):
        # Never expose absolute, escaping or otherwise invalid targets.
        return None


class _MeasuredContextBuilder(ContextBuilder):
    def __init__(
        self, metrics: EvalMetrics, budget: ContextBudget, counter: TokenCounter | None
    ) -> None:
        super().__init__(budget=budget, counter=counter)
        self.metrics = metrics
        counter_type = type(self.counter)
        metrics.token_counter_implementation = (
            f"{counter_type.__module__}.{counter_type.__qualname__}"
        )

    def build(self, **kwargs: Any) -> ContextRequest:
        # Forward every admission argument unchanged: this subclass observes the
        # base builder, it does not narrow what a caller may ask for.
        request = super().build(**kwargs)
        self.metrics.request_token_counts_estimated = (
            self.metrics.request_token_counts_estimated is True
            or request.tokens_after.is_estimate
        )
        self.metrics.context_trimmed_turns = max(
            self.metrics.context_trimmed_turns or 0, request.omitted_turns
        )
        self.metrics.context_trimmed_messages = max(
            self.metrics.context_trimmed_messages or 0, request.omitted_messages
        )
        return request


class EvalRunner:
    """Run one case with fresh runtime state and judge its final workspace.

    The caller supplies a fresh LLM factory and owns any tracer sink. This runner
    owns the case's observations and temporary directories; the optional temp_root
    is a parent directory which must already exist and is never removed by the
    runner. Supplied metrics stay live for interrupted reporting; returned metrics
    are detached snapshots. One runner executes one case at a time.
    """

    metrics_type: ClassVar[type[EvalMetrics]] = EvalMetrics

    #: Set by a subclass whose cases run with Project Memory. Passing the reader
    #: through ``build_agent`` lets Agent apply its workspace guard, instead of a
    #: subclass attaching a reader to an already-built Agent.
    memory_reader: "MemoryReader | None" = None

    def __init__(
        self,
        llm_factory: Callable[[], LLMClient],
        *,
        budget: RunBudget,
        run_timeout_seconds: float,
        check_timeout_seconds: float,
        context_budget: ContextBudget | None = None,
        tracer: Tracer | None = None,
        temp_root: Path | None = None,
        token_counter: TokenCounter | None = None,
        secret_env_keys: frozenset[str] = frozenset(),
    ) -> None:
        for name, value in (
            ("run_timeout_seconds", run_timeout_seconds),
            ("check_timeout_seconds", check_timeout_seconds),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and greater than zero")
        self.llm_factory = llm_factory
        self.budget = budget
        # Use the runner's explicit request allowance for every case.
        self.context_budget = (
            context_budget if context_budget is not None else ContextBudget()
        )
        self.run_timeout_seconds = run_timeout_seconds
        self.check_timeout_seconds = check_timeout_seconds
        self.tracer = tracer
        self.temp_root = temp_root
        self.token_counter = token_counter
        self.secret_env_keys = secret_env_keys
        self._running = False

    # The four extension points below are this class's lifecycle contract: a
    # subclass observes one case through them and is never expected to override
    # ``run``. ``run`` owns their order and guarantees, so a subclass may rely on
    # them and nothing else:
    #
    # * ``bind_metrics`` runs after the metrics record is reset to defaults and
    #   before any case work; the record it receives is the one that will be
    #   finalized, and it must be bound before observations are possible.
    # * ``prepare_workspace`` runs once per case on an empty temporary workspace
    #   whose lifetime the base class owns; it may add fixtures but not dispose
    #   of it.
    # * ``build_agent`` runs once per case after the workspace exists and before
    #   the turn starts, so instrumentation must be attached there.
    # * ``finalize_metrics`` runs after owned work settles, including when the
    #   case failed, and even when an earlier step raised; it must release
    #   subclass-owned resources and must not assume the case succeeded.
    #
    # A subclass owns only what these hooks identify: the case-specific
    # observation adapters and any resources it allocates for them.

    def bind_metrics(self, metrics: EvalMetrics) -> None:
        """Bind case-specific observation adapters before any case work starts."""

    def finalize_metrics(self, metrics: EvalMetrics) -> None:
        """Release case resources and finalize observations after work settles."""

    async def prepare_workspace(self, workspace: Workspace, case: EvalCase) -> None:
        """Materialize a case's fixtures into its fresh workspace."""
        for raw_path, text in case.files.items():
            path = workspace.resolve_path(raw_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def measured_context_builder(self, metrics: EvalMetrics) -> ContextBuilder:
        """The metrics-recording Context builder this runner installs.

        Declared as ``ContextBuilder`` so an override can return its own
        implementation instead of depending on this class's private one.
        """
        return _MeasuredContextBuilder(metrics, self.context_budget, self.token_counter)

    def build_agent(
        self,
        workspace: Workspace,
        llm: LLMClient,
        metrics: EvalMetrics,
        event_handler: EventHandler,
    ) -> Agent:
        """Assemble one case's Agent; subclasses wrap it to instrument it."""
        return build_agent(
            workspace=workspace,
            llm=llm,
            permission_handler=None,
            event_handler=event_handler,
            tracer=self.tracer,
            secret_env_keys=self.secret_env_keys,
            context_builder=self.measured_context_builder(metrics),
            memory_reader=self.memory_reader,
        )

    async def run(
        self,
        case: EvalCase,
        *,
        checks: Sequence[EvalCheck],
        metrics: EvalMetrics | None = None,
    ) -> EvalResult:
        if self._running:
            raise RuntimeError("EvalRunner is already running a case")
        metrics = metrics if metrics is not None else self.metrics_type()
        if not isinstance(metrics, self.metrics_type):
            raise TypeError(
                f"metrics must be an instance of {self.metrics_type.__name__}"
            )
        # A case's observations start empty, including every field a subclass
        # adds: resetting only the base fields would carry a previous case's
        # counters into this one.
        defaults = type(metrics)()
        for name in type(metrics).model_fields:
            setattr(metrics, name, getattr(defaults, name))
        started = perf_counter()
        self._running = True
        try:
            self.bind_metrics(metrics)
            result = await self._run(case, checks=checks, metrics=metrics)
        finally:
            try:
                self.finalize_metrics(metrics)
            finally:
                # Also available to suite reporting after cancellation cleanup.
                metrics.elapsed_seconds = perf_counter() - started
                self._running = False
        return result.model_copy(update={"metrics": metrics.model_copy(deep=True)})

    async def _run(
        self, case: EvalCase, *, checks: Sequence[EvalCheck], metrics: EvalMetrics
    ) -> EvalResult:
        checks = tuple(checks)
        results: list[CheckResult] = []
        execution_error: str | None = None
        failure_category: EvalFailureCategory | None = None
        trace_id: str | None = None

        def capture_trace(event: Event) -> None:
            nonlocal trace_id
            if event.type == "trace_start":
                trace_id = event.data["trace_id"]
                metrics.trace_id = trace_id
            elif event.type == "agent_step":
                metrics.agent_steps_used = event.data["step"]
            elif event.type == "trace_finish":
                usage = event.data.get("usage", {})
                metrics.provider_input_tokens = usage.get("input_tokens")
                metrics.provider_output_tokens = usage.get("output_tokens")

        if not checks:
            execution_error = "ValueError: at least one eval check is required"
            failure_category = EvalFailureCategory.RUNTIME_ERROR
        else:
            try:
                with TemporaryDirectory(
                    prefix="cairn-eval-", dir=self.temp_root
                ) as root:
                    workspace = Workspace(Path(root))
                    await self.prepare_workspace(workspace, case)

                    deadline = asyncio.timeout(self.run_timeout_seconds)
                    try:
                        llm = self.llm_factory()
                        model = getattr(llm, "model", None)
                        if isinstance(model, str):
                            metrics.model_identifier = model
                        output_limit = getattr(llm, "max_output_tokens", None)
                        if type(output_limit) is int and output_limit > 0:
                            metrics.provider_max_output_tokens = output_limit
                        agent = self.build_agent(workspace, llm, metrics, capture_trace)
                        async with deadline:
                            await run_turn(agent, case.prompt, budget=self.budget)
                    except Exception as exc:
                        execution_error = _error_message(exc)
                        failure_category = EvalFailureCategory.RUNTIME_ERROR
                        if isinstance(exc, TimeoutError) and deadline.expired():
                            failure_category = EvalFailureCategory.RUN_TIMEOUT
                            execution_error = (
                                "TimeoutError: eval run exceeded "
                                f"{self.run_timeout_seconds} seconds"
                            )
                        elif isinstance(
                            exc, (RunBudgetExceeded, ContextBudgetExceeded)
                        ):
                            failure_category = EvalFailureCategory.BUDGET_EXHAUSTED

                    # run_turn has returned or completed its cancellation cleanup.
                    # Preserve diagnostics even when execution already failed.
                    for index, check in enumerate(checks, start=1):
                        results.append(
                            await self._evaluate_check(check, workspace, index=index)
                        )
            except Exception as exc:
                error = _error_message(exc)
                execution_error = (
                    f"{execution_error}; {error}" if execution_error else error
                )
                failure_category = failure_category or EvalFailureCategory.RUNTIME_ERROR

        # External CancelledError is never caught above. Directory ownership
        # closes before returning a verdict, including on cancellation.
        if execution_error is not None or any(r.error is not None for r in results):
            status = EvalStatus.ERROR
            if failure_category is None:
                failure_category = next(
                    r.failure_category for r in results if r.error is not None
                )
        elif any(not r.passed for r in results):
            status = EvalStatus.FAIL
            failure_category = EvalFailureCategory.CHECK_FAILED
        else:
            status = EvalStatus.PASS

        return EvalResult(
            case_name=case.name,
            status=status,
            checks=results,
            trace_id=trace_id,
            error=execution_error,
            metrics=metrics,
            failure_category=failure_category,
        )

    async def _evaluate_check(
        self, check: EvalCheck, workspace: Workspace, *, index: int
    ) -> CheckResult:
        name = type(check).__name__
        target_path = _check_target_path(check, workspace)
        deadline = asyncio.timeout(self.check_timeout_seconds)
        try:
            async with deadline:
                check_name = check.name
                if not isinstance(check_name, str) or not check_name.strip():
                    raise ValueError("check name must be a non-empty string")
                name = check_name
                result = await check.evaluate(workspace)
                if not isinstance(result, CheckResult):
                    raise TypeError("check must return a CheckResult")
                category = (
                    EvalFailureCategory.CHECK_ERROR
                    if result.error is not None
                    else EvalFailureCategory.CHECK_FAILED
                    if not result.passed
                    else None
                )
                return result.model_copy(
                    update={
                        "check_index": index,
                        "check_type": type(check).__name__,
                        "target_path": target_path,
                        "failure_category": category,
                    }
                )
        except Exception as exc:
            error = _error_message(exc)
            category = EvalFailureCategory.CHECK_ERROR
            if isinstance(exc, TimeoutError) and deadline.expired():
                category = EvalFailureCategory.CHECK_TIMEOUT
                error = (
                    "TimeoutError: eval check exceeded "
                    f"{self.check_timeout_seconds} seconds"
                )
            return CheckResult(
                name=name,
                passed=False,
                error=error,
                check_index=index,
                check_type=type(check).__name__,
                target_path=target_path,
                failure_category=category,
            )
