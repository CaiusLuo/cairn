"""Bounded projections of Task results and existing Turn observations."""

from __future__ import annotations

import hashlib
import math
import os
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from cairn.core.events import Event, SessionEvent
from cairn.core.tasks import TaskResult, TaskStatus
from cairn.infra.observability.models import Span, SpanStatus
from cairn.infra.observability.storage import TraceStore
from cairn.infra.tools.credential_env import SECRET_ENV_KEYS

if TYPE_CHECKING:
    from cairn.runtime.evals.models import CheckResult

MAX_SUMMARY_BYTES = 4096
MAX_COUNT = 2**63 - 1
FailureCategory = Literal[
    "budget_exhausted",
    "cancelled",
    "inspection_failed",
    "model_error",
    "permission_error",
    "runtime_error",
    "timeout",
    "tool_error",
]
TraceDiagnostic = Literal["not_requested", "sink_failed", "unavailable", "incomplete"]


class TaskExecutionSummary(BaseModel):
    """Observations only: neither completion nor this artifact authorizes effects."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    schema_version: Literal[1] = 1
    task_id: str = Field(min_length=1, max_length=128)
    task_id_hashed: bool = False
    session_id: UUID | None = None
    turn_id: UUID | None = None
    trace_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    status: TaskStatus | None = None
    result_available: bool = False
    cancellation_propagated: bool = False
    elapsed_seconds: float | None = Field(default=None, ge=0)
    model_attempts: int | None = Field(default=None, ge=0, le=MAX_COUNT)
    tool_attempts: int | None = Field(default=None, ge=0, le=MAX_COUNT)
    tool_executions: int | None = Field(default=None, ge=0, le=MAX_COUNT)
    tool_failures: int | None = Field(default=None, ge=0, le=MAX_COUNT)
    permission_denials: int | None = Field(default=None, ge=0, le=MAX_COUNT)
    failure_category: FailureCategory | None = None
    timeout_observed: Literal[True] | None = None
    provider_input_tokens: int | None = Field(default=None, ge=0, le=MAX_COUNT)
    provider_output_tokens: int | None = Field(default=None, ge=0, le=MAX_COUNT)
    cost_usd: None = None
    trace_persisted: bool | None = None
    trace_diagnostic: TraceDiagnostic | None = "not_requested"
    verification_verdict: Literal["pass", "fail", "error"] | None = None
    verification_checks: int | None = Field(default=None, ge=1, le=MAX_COUNT)

    def to_json(self) -> str:
        """Export a validated, fixed-format artifact of at most 4 KiB."""
        validated = TaskExecutionSummary.model_validate(self.model_dump())
        text = validated.model_dump_json(indent=2) + "\n"
        if len(text.encode("utf-8")) > MAX_SUMMARY_BYTES:
            raise ValueError("Task summary exceeds export limit")
        return text


def _count(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= MAX_COUNT else None


class _TaskObservation:
    """Copies final Turn facts; does not count calls or aggregate token usage."""

    def __init__(self, task_id: str, secret_env_keys: frozenset[str]) -> None:
        self._secrets = tuple(
            value
            for name in SECRET_ENV_KEYS | secret_env_keys
            if (value := os.environ.get(name))
        )
        safe = re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", task_id) is not None and not any(
            secret in task_id for secret in self._secrets
        )
        self.task_id = (
            task_id
            if safe
            else "sha256:" + hashlib.sha256(task_id.encode()).hexdigest()
        )
        self.task_id_hashed = not safe
        self.session_id: UUID | None = None
        self.turn_id: UUID | None = None
        self.trace_id: str | None = None
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.persisted: bool | None = None
        self.timeout_observed: Literal[True] | None = None

    def _identity(self, value: object) -> UUID | None:
        if not isinstance(value, (str, UUID)):
            return None
        text = str(value)
        if any(secret in text for secret in self._secrets):
            return None
        try:
            return UUID(text)
        except ValueError:
            return None

    def observe(self, event: Event) -> None:
        if isinstance(event, SessionEvent):
            self.session_id = self._identity(event.session_id)
            self.turn_id = self._identity(event.turn_id)
        if event.type in ("trace_start", "trace_finish"):
            identity = self._identity(event.data.get("trace_id"))
            self.trace_id = identity.hex if identity is not None else None
        if event.type == "trace_finish":
            persisted = event.data.get("persisted")
            self.persisted = persisted if type(persisted) is bool else None
            usage = event.data.get("usage")
            self.input_tokens = (
                _count(usage.get("input_tokens")) if isinstance(usage, dict) else None
            )
            self.output_tokens = (
                _count(usage.get("output_tokens")) if isinstance(usage, dict) else None
            )

    def on_error(self, error: Exception) -> None:
        if isinstance(error, TimeoutError):
            self.timeout_observed = True

    def finish(
        self, result: TaskResult | None, elapsed: float | None, cancelled: bool
    ) -> TaskExecutionSummary:
        status = (
            result.status if result else TaskStatus.CANCELLED if cancelled else None
        )
        category: FailureCategory | None = None
        if status is TaskStatus.CANCELLED:
            category = "cancelled"
        elif status is TaskStatus.BUDGET_EXHAUSTED:
            category = "budget_exhausted"
        elif status is TaskStatus.RUNTIME_ERROR:
            if result is not None and result.repository.inspection_error is not None:
                category = "inspection_failed"
            else:
                category = "timeout" if self.timeout_observed else "runtime_error"
        if elapsed is not None and (not math.isfinite(elapsed) or elapsed < 0):
            elapsed = None
        return TaskExecutionSummary(
            task_id=self.task_id,
            task_id_hashed=self.task_id_hashed,
            session_id=self.session_id,
            turn_id=self.turn_id,
            trace_id=self.trace_id,
            status=status,
            result_available=result is not None,
            cancellation_propagated=cancelled,
            elapsed_seconds=elapsed,
            failure_category=category,
            timeout_observed=self.timeout_observed,
            provider_input_tokens=self.input_tokens,
            provider_output_tokens=self.output_tokens,
            trace_persisted=self.persisted,
            trace_diagnostic="sink_failed"
            if self.persisted is False
            else "not_requested",
        )


def _trace_metrics(
    summary: TaskExecutionSummary, spans: Sequence[Span]
) -> TaskExecutionSummary:
    roots = [s for s in spans if s.context.parent_span_id is None]
    if (
        len(roots) != 1
        or roots[0].name != "agent.turn"
        or any(
            s.context.trace_id != summary.trace_id or s.end_time is None for s in spans
        )
        or len({s.context.span_id for s in spans}) != len(spans)
    ):
        return summary.model_copy(update={"trace_diagnostic": "incomplete"})
    root = roots[0]
    relevant = [
        s
        for s in spans
        if s.name
        in {"llm.generate", "tool.execute", "tool.preflight", "permission.check"}
    ]
    if root.status not in (SpanStatus.OK, SpanStatus.ERROR) or any(
        s.context.parent_span_id != root.context.span_id for s in relevant
    ):
        return summary.model_copy(update={"trace_diagnostic": "incomplete"})
    executions = [s for s in relevant if s.name == "tool.execute"]
    preflights = [s for s in relevant if s.name == "tool.preflight"]
    permissions = [s for s in relevant if s.name == "permission.check"]
    category = summary.failure_category
    last = max(relevant, key=lambda s: s.end_time or s.start_time, default=None)
    if (
        category == "runtime_error"
        and last is not None
        and last.status is SpanStatus.ERROR
    ):
        categories: dict[str, FailureCategory] = {
            "llm.generate": "model_error",
            "tool.execute": "tool_error",
            "tool.preflight": "tool_error",
            "permission.check": "permission_error",
        }
        category = categories[last.name]
    return summary.model_copy(
        update={
            "model_attempts": sum(s.name == "llm.generate" for s in relevant),
            "tool_attempts": len(permissions) + len(preflights),
            "tool_executions": len(executions),
            "tool_failures": sum(
                s.status is SpanStatus.ERROR
                and s.attributes.get("cancelled") is not True
                for s in executions + preflights
            ),
            "permission_denials": sum(
                s.attributes.get("allowed") is False for s in permissions
            ),
            "failure_category": category,
            "timeout_observed": True
            if any(s.attributes.get("error_type") == "TimeoutError" for s in relevant)
            else summary.timeout_observed,
            "trace_diagnostic": None,
        }
    )


def summarize_task(
    summary: TaskExecutionSummary,
    *,
    trace_store: TraceStore | None = None,
    checks: Sequence[CheckResult] | None = None,
) -> TaskExecutionSummary:
    """Enrich a settled Task snapshot without executing tools or changing results.

    Check results must come from actual caller-owned deterministic verification.
    They are reduced to a verdict and count; no check text or metadata is copied.
    """
    if trace_store is not None and summary.trace_persisted is not False:
        try:
            if summary.trace_id is None:
                summary = summary.model_copy(update={"trace_diagnostic": "unavailable"})
            else:
                summary = _trace_metrics(summary, trace_store.read(summary.trace_id))
        except Exception:
            summary = summary.model_copy(update={"trace_diagnostic": "unavailable"})
    if checks:
        verdict = (
            "error"
            if any(c.error is not None for c in checks)
            else "pass"
            if all(c.passed for c in checks)
            else "fail"
        )
        summary = summary.model_copy(
            update={"verification_verdict": verdict, "verification_checks": len(checks)}
        )
    return summary
