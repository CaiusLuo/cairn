"""Offline doubles verify the compaction A/B orchestration, not a real model.

Every input is scripted, so a failure here is a defect in the comparison wiring
(isolation, snapshot fidelity, fact preservation, termination classification) and
never a provider's behaviour. Real-model effect is a separate, opt-in run.
"""

import asyncio
from collections.abc import Callable
from pathlib import Path

from cairn.core.budget import RunBudget
from cairn.core.compaction.summarizer import Summarizer, SummaryResult
from cairn.core.llm import LLMClient
from cairn.core.models import LLMResponse, Message, ToolCall
from cairn.runtime.evals.checks import FileContentEqualsCheck
from cairn.runtime.evals.compaction_ab import (
    Arm,
    CompactionCaseSpec,
    FailureKind,
    compare_case,
    failure_category,
)
from cairn.runtime.evals.models import EvalFailureCategory
from tests.support.runtime import SequenceLLM


def _spec(**overrides: object) -> CompactionCaseSpec:
    base = dict(
        name="continue-after-write",
        prompt="Write output.txt then confirm.",
        files={"input.txt": "seed\n"},
        current_instruction="finish the task",
        pre_side_effects={"applied.txt": "side effect already ran\n"},
        budget_tokens=2_000,
        response_tokens=200,
    )
    base.update(overrides)
    return CompactionCaseSpec(**base)  # type: ignore[arg-type]


class _CountingSummarizer(Summarizer):
    def __init__(self) -> None:
        self.calls: list[tuple[list[Message], int]] = []

    async def summarize(
        self, messages: list[Message], *, max_tokens: int
    ) -> SummaryResult:
        self.calls.append((messages, max_tokens))
        return SummaryResult(text="earlier work summarized; wrote applied.txt")


def _write_then_finish() -> list[LLMResponse]:
    return [
        LLMResponse(
            content=None,
            tool_calls=[
                ToolCall(
                    id="use-1",
                    name="apply_side_effect",
                    arguments={"value": 1},
                )
            ],
        ),
        LLMResponse(content="finished"),
    ]


def _run_factory(
    responses_fn: Callable[[], list[LLMResponse]],
) -> Callable[[], LLMClient]:
    """A client factory: one fresh scripted client per arm."""

    def factory() -> LLMClient:
        return SequenceLLM(responses_fn())

    return factory


def test_both_arms_terminate_and_report_independently(tmp_path: Path) -> None:
    spec = _spec()
    calls: list[SequenceLLM] = []

    def factory() -> LLMClient:
        client = SequenceLLM(_write_then_finish())
        calls.append(client)
        return client

    summarizer = _CountingSummarizer()
    report = asyncio.run(
        compare_case(
            spec,
            llm_factory=factory,
            budget=RunBudget(max_steps=4),
            run_timeout_seconds=10,
            summarizer_factory=lambda _llm: summarizer,
        )
    )
    assert report.both_ended_within_budget is True
    assert report.full_context.arm is Arm.FULL_CONTEXT
    assert report.real_summary.arm is Arm.REAL_SUMMARY
    assert report.full_context.answer == "finished"
    assert report.real_summary.answer == "finished"
    # Two arms => two independent clients, never one shared.
    assert len(calls) == 2 and calls[0] is not calls[1]
    # The instruction reached the model in both arms.
    assert report.full_context.instruction_reached_model is True
    assert report.real_summary.instruction_reached_model is True
    # Unknown provider usage stays null, not zero.
    assert report.full_context.provider_input_tokens is None


def test_each_arm_runs_in_its_own_temporary_workspace(tmp_path: Path) -> None:
    spec = _spec()
    report = asyncio.run(
        compare_case(
            spec,
            llm_factory=_run_factory(_write_then_finish),
            budget=RunBudget(max_steps=4),
            run_timeout_seconds=10,
            summarizer_factory=lambda _llm: _CountingSummarizer(),
        )
    )
    assert report.both_ended_within_budget is True
    # The side-effecting tool ran exactly once per arm; the pre-existing side
    # effect in the snapshot is never replayed by a second execution.
    assert report.full_context.side_effect_runs == 1
    assert report.real_summary.side_effect_runs == 1


def test_summary_arm_is_wired_and_full_arm_is_not() -> None:
    # On a case where both contexts fit, the only difference is that the summary
    # arm carries a summarizer while the baseline carries none. This proves the
    # wiring and the arm isolation without contradicting the "both fit" premise.
    spec = _spec(budget_tokens=5_000, response_tokens=200, older_history=("h" * 60,))
    summarizer = _CountingSummarizer()

    def factory() -> LLMClient:
        return SequenceLLM(_write_then_finish())

    report = asyncio.run(
        compare_case(
            spec,
            llm_factory=factory,
            budget=RunBudget(max_steps=4),
            run_timeout_seconds=10,
            summarizer_factory=lambda _llm: summarizer,
        )
    )
    assert report.both_ended_within_budget is True
    # The summary arm has a summarizer (calls measured, may be 0 when unneeded);
    # the baseline never had one, so its count is unknown (None), not zero.
    assert report.real_summary.summary_calls is not None
    assert report.full_context.summary_calls is None


def test_budget_exhaustion_is_classified_and_separated_from_correctness() -> None:
    spec = _spec()

    def factory() -> LLMClient:
        # Always asks for a tool and never finishes: the turn exhausts max_steps.
        return SequenceLLM(
            [
                LLMResponse(
                    tool_calls=[
                        ToolCall(id=f"c{i}", name="apply_side_effect", arguments={})
                    ]
                )
                for i in range(20)
            ]
        )

    checks = (FileContentEqualsCheck("applied.txt", "side effect already ran\n"),)
    report = asyncio.run(
        compare_case(
            spec,
            llm_factory=factory,
            checks=checks,
            budget=RunBudget(max_steps=2),
            run_timeout_seconds=10,
            summarizer_factory=lambda _llm: _CountingSummarizer(),
        )
    )
    assert report.both_ended_within_budget is False
    assert report.full_context.failure is FailureKind.BUDGET_EXHAUSTED
    assert report.real_summary.failure is FailureKind.BUDGET_EXHAUSTED
    # Correctness is not claimed when an arm did not terminate.
    assert report.final_workspace_correct == {}
    assert report.notes


def test_final_workspace_correctness_is_assessed_per_check() -> None:
    spec = _spec()
    checks = (FileContentEqualsCheck("applied.txt", "side effect already ran\n"),)

    def factory() -> LLMClient:
        return SequenceLLM(_write_then_finish())

    report = asyncio.run(
        compare_case(
            spec,
            llm_factory=factory,
            checks=checks,
            budget=RunBudget(max_steps=4),
            run_timeout_seconds=10,
            summarizer_factory=lambda _llm: _CountingSummarizer(),
        )
    )
    assert report.both_ended_within_budget is True
    assert report.final_workspace_correct == {"FileContentEqualsCheck": True}


def test_failure_category_mapping() -> None:
    assert failure_category(FailureKind.NONE) is None
    assert failure_category(FailureKind.RUN_TIMEOUT) is EvalFailureCategory.RUN_TIMEOUT
    assert (
        failure_category(FailureKind.BUDGET_EXHAUSTED)
        is EvalFailureCategory.BUDGET_EXHAUSTED
    )
    assert (
        failure_category(FailureKind.RUNTIME_ERROR) is EvalFailureCategory.RUNTIME_ERROR
    )
