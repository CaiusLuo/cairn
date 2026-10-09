import asyncio
import math
from collections.abc import Callable, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Any

from cairn.assembly import build_agent
from cairn.core.budget import RunBudget
from cairn.core.context import (
    ContextBudget,
    ContextBuilder,
    ContextRequest,
    TokenCounter,
)
from cairn.core.events import Event
from cairn.core.loop import run_turn
from cairn.core.models import Message
from cairn.evals.models import (
    CheckResult,
    EvalCase,
    EvalCheck,
    EvalMetrics,
    EvalResult,
    EvalStatus,
)
from cairn.llm.base import LLMClient
from cairn.observability.tracer import Tracer
from cairn.workspace.workspace import Workspace


def _error_message(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


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

    def build(
        self,
        *,
        system_messages: list[Message],
        history: list[Message],
        current_turn_start: int,
        tools: list[dict[str, Any]],
    ) -> ContextRequest:
        request = super().build(
            system_messages=system_messages,
            history=history,
            current_turn_start=current_turn_start,
            tools=tools,
        )
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
    owns only its temporary case directories; the optional temp_root is a parent
    directory which must already exist and is never removed by the runner.
    """

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

    async def run(
        self,
        case: EvalCase,
        *,
        checks: Sequence[EvalCheck],
        metrics: EvalMetrics | None = None,
    ) -> EvalResult:
        metrics = metrics if metrics is not None else EvalMetrics()
        for name in EvalMetrics.model_fields:
            setattr(metrics, name, None)
        started = perf_counter()
        try:
            return await self._run(case, checks=checks, metrics=metrics)
        finally:
            # Also available to suite reporting after cancellation cleanup.
            metrics.elapsed_seconds = perf_counter() - started

    async def _run(
        self, case: EvalCase, *, checks: Sequence[EvalCheck], metrics: EvalMetrics
    ) -> EvalResult:
        checks = tuple(checks)
        results: list[CheckResult] = []
        execution_error: str | None = None
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
        else:
            try:
                with TemporaryDirectory(
                    prefix="cairn-eval-", dir=self.temp_root
                ) as root:
                    workspace = Workspace(Path(root))
                    for raw_path, text in case.files.items():
                        path = workspace.resolve_path(raw_path)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(text, encoding="utf-8")

                    deadline = asyncio.timeout(self.run_timeout_seconds)
                    try:
                        llm = self.llm_factory()
                        model = getattr(llm, "model", None)
                        if isinstance(model, str):
                            metrics.model_identifier = model
                        output_limit = getattr(llm, "max_output_tokens", None)
                        if type(output_limit) is int and output_limit > 0:
                            metrics.provider_max_output_tokens = output_limit
                        agent = build_agent(
                            workspace=workspace,
                            llm=llm,
                            # Evals deliberately run with baseline-only authority.
                            permission_handler=None,
                            event_handler=capture_trace,
                            tracer=self.tracer,
                            context_builder=_MeasuredContextBuilder(
                                metrics, self.context_budget, self.token_counter
                            ),
                        )
                        async with deadline:
                            await run_turn(agent, case.prompt, budget=self.budget)
                    except Exception as exc:
                        execution_error = _error_message(exc)
                        if isinstance(exc, TimeoutError) and deadline.expired():
                            execution_error = (
                                "TimeoutError: eval run exceeded "
                                f"{self.run_timeout_seconds} seconds"
                            )

                    # run_turn has returned or completed its cancellation cleanup.
                    # Preserve diagnostics even when execution already failed.
                    for check in checks:
                        results.append(await self._evaluate_check(check, workspace))
            except Exception as exc:
                error = _error_message(exc)
                execution_error = (
                    f"{execution_error}; {error}" if execution_error else error
                )

        # External CancelledError is never caught above. Directory ownership
        # closes before returning a verdict, including on cancellation.
        if execution_error is not None or any(r.error is not None for r in results):
            status = EvalStatus.ERROR
        elif any(not r.passed for r in results):
            status = EvalStatus.FAIL
        else:
            status = EvalStatus.PASS

        return EvalResult(
            case_name=case.name,
            status=status,
            checks=results,
            trace_id=trace_id,
            error=execution_error,
            metrics=metrics,
        )

    async def _evaluate_check(
        self, check: EvalCheck, workspace: Workspace
    ) -> CheckResult:
        name = type(check).__name__
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
                return result
        except Exception as exc:
            error = _error_message(exc)
            if isinstance(exc, TimeoutError) and deadline.expired():
                error = (
                    "TimeoutError: eval check exceeded "
                    f"{self.check_timeout_seconds} seconds"
                )
            return CheckResult(name=name, passed=False, error=error)
