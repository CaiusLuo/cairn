"""Real-summary vs full-context continuation comparison for one compaction case.

The offline scripted suite (``evaluation/cases/context_compaction.py`` and
``evaluation/runners/run_context_compaction.py``) already proves Cairn's own
compaction *mechanism*: it must not lose or replay executed facts. This module
answers a different, opt-in question that only a real model can answer: when a
task's conversation fits the budget either way, does continuing from a real
model summary behave as well as continuing with the full context?

It is deliberately an *orchestrator*, not a second execution engine. Both arms
run through the one lifecycle owner, ``runtime.loop.run_turn``, in **independent
throwaway workspaces** built from the same pre-compaction snapshot. The offline
doubles in the tests reuse this same orchestration, so a test failure is a defect
in the comparison wiring, never a model's mood.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Any

from cairn.core.budget import RunBudget
from cairn.core.compaction.summarizer import Summarizer
from cairn.core.context import ContextBudget, ContextBuilder, TokenCount, TokenCounter
from cairn.core.events import EventHandler
from cairn.core.llm import LLMClient
from cairn.core.models import Message, ToolResult
from cairn.core.permissions import PermissionHandler
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.assembly import build_agent
from cairn.runtime.evals.models import EvalCheck, EvalFailureCategory
from cairn.runtime.loop import run_turn


class Arm(StrEnum):
    """The two continuation strategies compared on identical inputs."""

    #: Continue with the complete, uncompacted context (the baseline).
    FULL_CONTEXT = "full_context"
    #: Continue after a real model summary replaced the older history.
    REAL_SUMMARY = "real_summary"


class FailureKind(StrEnum):
    """Why an arm did not end within budget; ``NONE`` means it ended."""

    NONE = "none"
    RUN_TIMEOUT = "run_timeout"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CONTEXT_EXCEEDED = "context_exceeded"
    RUNTIME_ERROR = "runtime_error"
    CANCELLED = "cancelled"
    SETUP_ERROR = "setup_error"


@dataclass(frozen=True, slots=True)
class CompactionCaseSpec:
    """One pre-snapshot task plus the facts that must survive either arm.

    ``files`` is the pre-compaction workspace snapshot (state the model sees
    before continuing). ``pre_snapshot_fingerprint`` and ``pre_side_effects``
    describe already-executed work that a continuation must neither lose nor
    re-run; the offline doubles assert against them.
    """

    name: str
    prompt: str
    files: dict[str, str]
    #: Text the operator typed; it must reach the model verbatim in both arms.
    current_instruction: str
    #: Files an already-executed side effect produced before continuing. Their
    #: content must be present but the effect must not be re-run by a tool.
    pre_side_effects: dict[str, str] = field(default_factory=dict)
    #: Text that a summary may compress but whose decisions must survive.
    older_history: tuple[str, ...] = ()
    budget_tokens: int = 8_000
    response_tokens: int = 1_024
    #: A short deterministic system prompt so a character counter is readable.
    system_prompt: str = "sys"


@dataclass(slots=True)
class ArmObservation:
    """What one arm actually did; unknown values stay ``None``."""

    arm: Arm
    answer: str | None = None
    ended_within_budget: bool | None = None
    failure: FailureKind = FailureKind.NONE
    error: str | None = None
    checks: tuple[EvalCheck, ...] = ()
    checks_passed: bool | None = None
    #: Per-check outcomes by name; empty until checks run.
    check_results: dict[str, bool] = field(default_factory=dict)
    tool_run_count: int | None = None
    side_effect_runs: int | None = None
    instruction_reached_model: bool | None = None
    summary_calls: int | None = None
    provider_input_tokens: int | None = None
    provider_output_tokens: int | None = None
    elapsed_seconds: float | None = None
    model_identifier: str | None = None
    model_version: str | None = None


@dataclass(slots=True)
class ComparisonReport:
    """Both arms for one case, with correctness and termination kept separate."""

    case_name: str
    full_context: ArmObservation
    real_summary: ArmObservation
    #: ``None`` when either arm failed to terminate and no fair comparison exists.
    both_ended_within_budget: bool | None = None
    #: Per-check outcome keyed by check name; ``None`` when not both ran checks.
    final_workspace_correct: dict[str, bool | None] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


class _CharCounter:
    """One token per character so a budget is a readable, deterministic number."""

    def count(self, messages: list[Message], tools: list[dict[str, Any]]) -> TokenCount:
        return TokenCount(
            tokens=sum(len(message.content or "") for message in messages),
            is_estimate=False,
        )


def _check_name(check: EvalCheck) -> str:
    label = getattr(check, "check_type", None)
    return label if isinstance(label, str) and label.strip() else type(check).__name__


class _SideEffectTrackingTool:
    """A ``bash``-registered tool that records how often it actually ran.

    The tool name is not ``bash`` in the non-permission case; it is registered
    under its own name. It counts executions so a replayed side effect after
    compaction is visible regardless of the model's claims.
    """

    def __init__(self, name: str, output: str) -> None:
        self.name = name
        self.description = "Return a deterministic side-effecting result."
        self.output = output
        self.runs = 0

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description}

    def validate(self, arguments: dict[str, Any]) -> None:
        pass

    async def execute(self, arguments: dict[str, Any], **_: Any) -> ToolResult:
        self.runs += 1
        return ToolResult(stdout=self.output, exit_code=0)


async def _run_arm(
    *,
    spec: CompactionCaseSpec,
    arm: Arm,
    llm_factory: Callable[[], LLMClient],
    summarizer_factory: Callable[[LLMClient], Summarizer | None] | None,
    checks: Sequence[EvalCheck],
    budget: RunBudget,
    run_timeout_seconds: float,
    temp_root: Path | None,
    permission_handler: PermissionHandler | None,
    event_handler: EventHandler | None,
    token_counter: TokenCounter | None,
) -> ArmObservation:
    observation = ArmObservation(arm=arm, checks=tuple(checks))
    started = perf_counter()
    with TemporaryDirectory(prefix=f"cairn-cmp-{arm.value}-", dir=temp_root) as root:
        workspace = Workspace(Path(root))
        try:
            for raw_path, text in spec.files.items():
                path = workspace.resolve_path(raw_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            for raw_path, text in spec.pre_side_effects.items():
                path = workspace.resolve_path(raw_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
        except Exception as exc:
            observation.failure = FailureKind.SETUP_ERROR
            observation.error = f"{type(exc).__name__}: {exc}"
            observation.ended_within_budget = False
            return observation

        llm = llm_factory()
        model = getattr(llm, "model", None)
        if isinstance(model, str):
            observation.model_identifier = model
        summarizer = summarizer_factory(llm) if summarizer_factory else None

        builder = ContextBuilder(
            budget=ContextBudget(
                max_tokens=spec.budget_tokens, response_tokens=spec.response_tokens
            ),
            counter=token_counter or _CharCounter(),
        )
        agent = build_agent(
            workspace=workspace,
            llm=llm,
            permission_handler=permission_handler,
            event_handler=event_handler,
            tracer=None,
            context_builder=builder,
            summarizer=summarizer,
        )
        # A short deterministic system prompt keeps the character-counter budget
        # readable and identical between arms.
        agent.system_prompt = spec.system_prompt
        side_effect_tool = _SideEffectTrackingTool("apply_side_effect", "applied")
        agent.tools.register_tool(side_effect_tool)
        for older in spec.older_history:
            agent.state.add_user_message(older)
            agent.state.add_assistant_message("acknowledged")

        deadline = asyncio.timeout(run_timeout_seconds)
        try:
            async with deadline:
                observation.answer = await run_turn(
                    agent, spec.current_instruction, budget=budget
                )
            observation.ended_within_budget = True
        except asyncio.CancelledError:
            observation.failure = FailureKind.CANCELLED
            observation.error = "CancelledError"
            observation.ended_within_budget = False
            raise
        except TimeoutError as exc:
            if deadline.expired():
                observation.failure = FailureKind.RUN_TIMEOUT
                observation.error = f"TimeoutError: {exc}"
            else:
                observation.failure = FailureKind.RUNTIME_ERROR
                observation.error = f"TimeoutError: {exc}"
            observation.ended_within_budget = False
        except Exception as exc:
            observation.ended_within_budget = False
            message = f"{type(exc).__name__}: {exc}"
            observation.error = message
            name = type(exc).__name__
            if name == "RunBudgetExceeded":
                observation.failure = FailureKind.BUDGET_EXHAUSTED
            elif name == "ContextBudgetExceeded":
                observation.failure = FailureKind.CONTEXT_EXCEEDED
            else:
                observation.failure = FailureKind.RUNTIME_ERROR

        # Facts recorded on every terminal path, including failure/cancel.
        observation.tool_run_count = side_effect_tool.runs
        observation.side_effect_runs = side_effect_tool.runs
        observation.instruction_reached_model = any(
            message.role == "user" and message.content == spec.current_instruction
            for message in agent.state.messages
        )
        observation.summary_calls = _summarizer_calls(summarizer)
        observation.elapsed_seconds = perf_counter() - started

        # Judge final workspace behaviour independently of the model's answer.
        try:
            observation.check_results = await _run_checks(
                checks, workspace, run_timeout_seconds
            )
            observation.checks_passed = all(observation.check_results.values())
        except Exception as exc:
            observation.checks_passed = False
            observation.error = (
                f"{observation.error}; check error: {type(exc).__name__}"
                if observation.error
                else f"check error: {type(exc).__name__}"
            )
    return observation


async def _run_checks(
    checks: Sequence[EvalCheck], workspace: Workspace, timeout: float
) -> dict[str, bool]:
    results: dict[str, bool] = {}
    for check in checks:
        async with asyncio.timeout(timeout):
            result = await check.evaluate(workspace)
        results[_check_name(check)] = bool(result.passed and result.error is None)
    return results


def _summarizer_calls(summarizer: Summarizer | None) -> int | None:
    calls = getattr(summarizer, "calls", None)
    if isinstance(calls, list):
        return len(calls)
    if isinstance(calls, int):
        return calls
    return None


async def compare_case(
    spec: CompactionCaseSpec,
    *,
    llm_factory: Callable[[], LLMClient],
    checks: Sequence[EvalCheck] = (),
    budget: RunBudget,
    run_timeout_seconds: float = 120.0,
    check_timeout_seconds: float = 10.0,
    temp_root: Path | None = None,
    permission_handler: PermissionHandler | None = None,
    event_handler: EventHandler | None = None,
    token_counter: TokenCounter | None = None,
    summarizer_factory: Callable[[LLMClient], Summarizer | None] | None = None,
) -> ComparisonReport:
    """Run both arms on identical pre-snapshot inputs in independent directories.

    ``FULL_CONTEXT`` runs with no summarizer and a high enough budget that the
    raw request is admitted. ``REAL_SUMMARY`` uses the supplied summarizer so the
    older history can be replaced. Both arms share ``run_turn`` as the only
    lifecycle; this function only orchestrates and observes.

    The default summarizer factory returns the client's own summarizer (real
    model) only when one is configured; offline callers pass a double.
    """
    checks = tuple(checks)

    full = await _run_arm(
        spec=spec,
        arm=Arm.FULL_CONTEXT,
        llm_factory=llm_factory,
        summarizer_factory=None,  # baseline: no compaction
        checks=checks,
        budget=budget,
        run_timeout_seconds=run_timeout_seconds,
        temp_root=temp_root,
        permission_handler=permission_handler,
        event_handler=event_handler,
        token_counter=token_counter,
    )
    summary = await _run_arm(
        spec=spec,
        arm=Arm.REAL_SUMMARY,
        llm_factory=llm_factory,
        summarizer_factory=summarizer_factory or (lambda client: None),
        checks=checks,
        budget=budget,
        run_timeout_seconds=run_timeout_seconds,
        temp_root=temp_root,
        permission_handler=permission_handler,
        event_handler=event_handler,
        token_counter=token_counter,
    )

    report = ComparisonReport(
        case_name=spec.name, full_context=full, real_summary=summary
    )
    both_terminated = (
        full.ended_within_budget is True and summary.ended_within_budget is True
    )
    report.both_ended_within_budget = both_terminated
    if both_terminated:
        # Correctness is only comparable when both arms actually ran to an end.
        names = set(full.check_results) | set(summary.check_results)
        report.final_workspace_correct = {
            name: full.check_results.get(name) is True
            and summary.check_results.get(name) is True
            for name in sorted(names)
        }
    else:
        report.notes.append(
            "At least one arm did not end within budget; a fair full-vs-summary "
            "correctness comparison is not available."
        )
    return report


def failure_category(kind: FailureKind) -> EvalFailureCategory | None:
    """Map an arm failure to the eval failure vocabulary for reporting."""
    if kind is FailureKind.NONE:
        return None
    if kind is FailureKind.RUN_TIMEOUT:
        return EvalFailureCategory.RUN_TIMEOUT
    if kind is FailureKind.BUDGET_EXHAUSTED:
        return EvalFailureCategory.BUDGET_EXHAUSTED
    return EvalFailureCategory.RUNTIME_ERROR
