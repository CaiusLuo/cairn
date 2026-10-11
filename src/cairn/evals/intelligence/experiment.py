"""Finite seeded four-arm orchestration; no production feature activation."""

import asyncio
import random
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from cairn.core.context import TokenCounter
from cairn.core.models import LLMResponse, Message
from cairn.evals.intelligence.dataset import VERSION, dataset_digest
from cairn.evals.intelligence.fixtures import IntelligenceCase
from cairn.evals.intelligence.models import (
    Arm,
    ExperimentConfig,
    ExperimentReport,
    ExperimentRun,
    IntelligenceMetrics,
)
from cairn.evals.intelligence.reporting import persist
from cairn.evals.intelligence.runner import IntelligenceRunner, SafeTraceSink
from cairn.evals.models import EvalFailureCategory, EvalResult, EvalStatus
from cairn.evals.suite import _report_result
from cairn.llm.base import LLMClient
from cairn.observability.tracer import Tracer


class ObservedLLM:
    def __init__(self, client: LLMClient, metrics: IntelligenceMetrics) -> None:
        self.client = client
        self.metrics = metrics
        self.model = getattr(client, "model", None)
        self.max_output_tokens = getattr(client, "max_output_tokens", None)

    async def generate(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None
    ) -> LLMResponse:
        self.metrics.provider_requests += 1
        try:
            response = await self.client.generate(messages, tools)
        except Exception:
            self.metrics.provider_failures += 1
            raise RuntimeError("Model request failed.") from None
        self.metrics.provider_responses += 1
        return response


def execution_plan(
    cases: tuple[IntelligenceCase, ...], seed: int, repetitions: int
) -> list[tuple[IntelligenceCase, Arm, int]]:
    if not 1 <= repetitions <= 3 or not 1 <= len(cases) <= 6:
        raise ValueError("Experiment allows 1-6 cases and 1-3 repetitions")
    if len({spec.item.case.name for spec in cases}) != len(cases):
        raise ValueError("Experiment case names must be unique")
    rng = random.Random(seed)
    plan: list[tuple[IntelligenceCase, Arm, int]] = []
    for repetition in range(1, repetitions + 1):
        shuffled = list(cases)
        rng.shuffle(shuffled)
        for spec in shuffled:
            arms = list(Arm)
            rng.shuffle(arms)
            plan.extend((spec, arm, repetition) for arm in arms)
    return plan


async def run_experiment(
    cases: tuple[IntelligenceCase, ...],
    config: ExperimentConfig,
    llm_factory: Callable[[], LLMClient],
    *,
    directory: Path,
    token_counter: TokenCounter | None = None,
    secret_env_keys: frozenset[str] = frozenset(),
    temp_root: Path | None = None,
    memory_temp_root: Path | None = None,
    on_result: Callable[[ExperimentRun], None] | None = None,
) -> ExperimentReport:
    if (
        config.dataset_version != VERSION
        or config.dataset_sha256 != dataset_digest(cases)
        or config.case_names != tuple(spec.item.case.name for spec in cases)
    ):
        raise ValueError("Experiment configuration does not match the frozen dataset")
    report = ExperimentReport(config=config)
    plan = execution_plan(cases, config.seed, config.repetitions)
    persist(report, directory)
    for index, (spec, arm, repetition) in enumerate(plan):
        run_id = f"{index + 1:03d}-{spec.item.case.name}-{arm.value}-r{repetition}"
        metrics = IntelligenceMetrics()
        report.active_run_id, report.active_metrics = run_id, metrics
        persist(report, directory)
        runner = IntelligenceRunner(
            lambda metrics=metrics: ObservedLLM(llm_factory(), metrics),
            spec=spec,
            arm=arm,
            observations=metrics,
            budget=config.settings.run_budget,
            context_budget=config.settings.context_budget,
            run_timeout_seconds=config.settings.run_timeout_seconds,
            check_timeout_seconds=config.settings.check_timeout_seconds,
            tool_timeout_seconds=config.tool_timeout_seconds,
            tracer=Tracer(SafeTraceSink(directory / "traces", metrics)),
            token_counter=token_counter,
            secret_env_keys=secret_env_keys,
            temp_root=temp_root,
            memory_temp_root=memory_temp_root,
        )
        task = asyncio.create_task(runner.run_case())
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.done():
                task.cancel()
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if task.done() and not task.cancelled():
                # Consume cleanup errors without persisting their raw messages.
                task.exception()
            report.state = "interrupted"
            with suppress(Exception):
                persist(report, directory)
            raise
        except Exception:
            result = EvalResult(
                case_name=spec.item.case.name,
                status=EvalStatus.ERROR,
                error="Case execution failed.",
                failure_category=EvalFailureCategory.RUNTIME_ERROR,
            )
        run = ExperimentRun(
            run_id=run_id,
            case_name=spec.item.case.name,
            arm=arm,
            repetition=repetition,
            order_index=index,
            result=_report_result(result),
            metrics=metrics,
        )
        report.runs.append(run)
        report.active_run_id = report.active_metrics = None
        # A provider failure stops this finite pilot instead of making additional
        # paid/unavailable requests. Completed evidence remains available.
        if config.execution_kind == "real" and metrics.provider_failures:
            report.state = "real_eval_blocked"
            report.real_provider_status = "BLOCKED"
        elif len(report.runs) == len(plan):
            report.state = "completed"
            if config.execution_kind == "real":
                report.real_provider_status = (
                    "PASS"
                    if all(
                        item.result.status is EvalStatus.PASS for item in report.runs
                    )
                    else "FAIL"
                )
        persist(report, directory)
        if on_result:
            on_result(run)
        if report.state == "real_eval_blocked":
            break
    return report
