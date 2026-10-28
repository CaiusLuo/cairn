"""Offline doubles verify the compaction A/B orchestration, not a real model.

Every input is scripted, so a failure here is a defect in the comparison wiring
(isolation, snapshot fidelity, fact preservation, termination classification) and
never a provider's behaviour. Real-model effect is a separate, opt-in run.
"""

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from cairn.core.budget import RunBudget
from cairn.core.compaction.summarizer import Summarizer, SummaryResult
from cairn.core.context import TokenCount
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


# ============================================================================
# Issue 1: hard total-token budget (shared across arms and summarizer calls)
# ============================================================================

from cairn.runtime.evals.compaction_ab import (  # noqa: E402
    BudgetedLLM,
    TokenBudgetLedger,
    TotalBudgetExceeded,
    _CharCounter,
)


class _UsageClient:
    """A minimal LLMClient double with fixed usage; records call count."""

    model = "double-model"
    max_output_tokens = 50

    def __init__(self, usage: "object | None") -> None:
        self.usage = usage
        self.calls: list[list[Message]] = []

    async def generate(
        self, messages: list[Message], tools: list[dict[str, object]] | None = None
    ) -> LLMResponse:
        self.calls.append(messages)
        return LLMResponse(content="ok", usage=self.usage)  # type: ignore[arg-type]


class _ConstCounter:
    def __init__(self, per_message: int = 5) -> None:
        self.per_message = per_message

    def count(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> TokenCount:
        return TokenCount(tokens=self.per_message * len(messages), is_estimate=False)


def test_ledger_reserves_before_spending_and_refuses_when_unaffordable() -> None:
    ledger = TokenBudgetLedger(total=100)
    assert ledger.reserve(10, 20) == 30  # affordable
    ledger.settle(30, actual_known=True)
    assert ledger.remaining == 70
    with pytest.raises(TotalBudgetExceeded):
        ledger.reserve(60, 20)  # 80 > 70


def test_unknown_usage_is_charged_not_zero() -> None:
    ledger = TokenBudgetLedger(total=1000)
    client = _UsageClient(usage=None)
    wrapped = BudgetedLLM(client, ledger, _ConstCounter(10), output_cap=50)
    asyncio.run(wrapped.generate([Message(role="user", content="hi")]))
    # Unknown usage is charged the reservation and flagged, never treated as zero.
    assert ledger.spent > 0
    assert ledger.unknown_usage is True
    assert wrapped.usage_totals() == (None, None)


def test_summary_request_is_counted_against_the_same_ledger() -> None:
    ledger = TokenBudgetLedger(total=1000)
    client = _UsageClient(usage=None)
    wrapped = BudgetedLLM(client, ledger, _ConstCounter(10), output_cap=50)
    summary_messages = [
        Message(role="system", content="You are summarizing one conversation slice."),
        Message(role="user", content="history"),
    ]
    asyncio.run(wrapped.generate(summary_messages))
    assert wrapped.summary_request_count() == 1
    # The summarizer call consumed budget like any other request.
    assert ledger.spent > 0


def test_low_total_budget_refused_before_any_provider_call(tmp_path: Path) -> None:
    spec = _spec()
    clients: list[SequenceLLM] = []

    def factory() -> LLMClient:
        client = SequenceLLM(_write_then_finish())
        clients.append(client)
        return client

    report = asyncio.run(
        compare_case(
            spec,
            llm_factory=factory,
            budget=RunBudget(max_steps=4),
            total_token_budget=5,  # far below one request
            run_timeout_seconds=10,
            summarizer_factory=lambda _llm: _CountingSummarizer(),
        )
    )
    assert report.both_ended_within_budget is False
    assert report.full_context.failure is FailureKind.TOTAL_BUDGET_EXCEEDED
    assert report.real_summary.failure is FailureKind.TOTAL_BUDGET_EXCEEDED
    # Nothing was ever sent to a provider.
    assert all(client.calls == [] for client in clients)
    assert report.tokens_spent_total == 0


def test_two_arms_share_one_total_budget(tmp_path: Path) -> None:
    spec = _spec()
    # Measure the full arm's exact consumption with a generous cap.
    measured = asyncio.run(
        compare_case(
            spec,
            llm_factory=_run_factory(_write_then_finish),
            budget=RunBudget(max_steps=4),
            total_token_budget=10**9,
            run_timeout_seconds=10,
            summarizer_factory=lambda _llm: _CountingSummarizer(),
        )
    )
    full_spend = measured.full_context.tokens_spent
    assert full_spend is not None and full_spend > 0
    # A cap equal to the full arm's spend lets the full arm finish, then refuses
    # the summary arm at its first reservation (shared ledger).
    report = asyncio.run(
        compare_case(
            spec,
            llm_factory=_run_factory(_write_then_finish),
            budget=RunBudget(max_steps=4),
            total_token_budget=full_spend,
            run_timeout_seconds=10,
            summarizer_factory=lambda _llm: _CountingSummarizer(),
        )
    )
    assert report.full_context.ended_within_budget is True
    assert report.real_summary.failure is FailureKind.TOTAL_BUDGET_EXCEEDED
    assert report.both_ended_within_budget is False
    assert report.tokens_spent_total == full_spend


# ============================================================================
# Issue 2: a valid comparison requires real compaction and preserved facts
# ============================================================================


class _KeySummarizer(Summarizer):
    def __init__(self, text: str) -> None:
        self.calls: list[tuple[list[Message], int]] = []
        self.text = text

    async def summarize(
        self, messages: list[Message], *, max_tokens: int
    ) -> SummaryResult:
        self.calls.append((messages, max_tokens))
        return SummaryResult(text=self.text)


def _deploy_spec(**overrides: object) -> CompactionCaseSpec:
    history = (
        Message(
            role="user",
            content=(
                "Prior analysis "
                + "A" * 300
                + " DEPLOY-ANALYSIS-7781 and conclusion SEED-DEPLOY-DONE-4471"
            ),
        ),
        Message(role="assistant", content="acknowledged"),
        Message(
            role="assistant",
            content=None,
            tool_calls=[ToolCall(id="seed-1", name="apply_side_effect", arguments={})],
        ),
        Message(
            role="tool",
            content="deployment applied and appended to the log; do not repeat",
            tool_call_id="seed-1",
        ),
    )
    base = dict(
        name="verify-after-deploy",
        prompt="Verify the deployment log and report finished.",
        files={"applied_log.txt": "applied\n"},
        current_instruction="Verify applied_log.txt and report finished. Do not deploy again.",
        history=history,
        budget_tokens=8_000,
        summary_budget_tokens=1_600,
        response_tokens=150,
        side_effect_log="applied_log.txt",
        forbidden_history_marker="DEPLOY-ANALYSIS-7781",
        key_fact_marker="SEED-DEPLOY-DONE-4471",
        expected_side_effect_runs=0,
    )
    base.update(overrides)
    return CompactionCaseSpec(**base)  # type: ignore[arg-type]


def _inspect_program(
    count: int, extra: list[LLMResponse] | None = None
) -> list[LLMResponse]:
    responses = [
        LLMResponse(
            content=None,
            tool_calls=[ToolCall(id=f"i{i}", name="inspect", arguments={})],
        )
        for i in range(count)
    ]
    responses += extra or []
    responses.append(LLMResponse(content="finished"))
    return responses


def _deploy_checks() -> tuple[FileContentEqualsCheck, ...]:
    return (FileContentEqualsCheck("applied_log.txt", "applied\n"),)


def test_valid_when_compaction_occurs_and_facts_preserved(tmp_path: Path) -> None:
    summ = _KeySummarizer(
        "earlier deploy summarized; SEED-DEPLOY-DONE-4471 applied; do not repeat"
    )
    report = asyncio.run(
        compare_case(
            _deploy_spec(),
            llm_factory=_run_factory(lambda: _inspect_program(3)),
            checks=_deploy_checks(),
            budget=RunBudget(max_steps=12),
            run_timeout_seconds=20,
            summarizer_factory=lambda _llm: summ,
        )
    )
    assert report.both_ended_within_budget is True
    assert report.real_summary.compaction_occurred is True
    assert report.real_summary.compacted_history_used is True
    assert report.real_summary.key_fact_preserved is True
    assert report.real_summary.replayed_side_effect is False
    assert report.valid is True, report.invalid_reasons
    assert report.final_workspace_correct == {"FileContentEqualsCheck": True}


def test_invalid_when_summary_arm_does_not_compact(tmp_path: Path) -> None:
    # A generous summary budget means folding/omission suffices, so the
    # summarizer never runs; the run must be marked invalid, not successful.
    report = asyncio.run(
        compare_case(
            _deploy_spec(summary_budget_tokens=2_600),
            llm_factory=_run_factory(lambda: _inspect_program(3)),
            checks=_deploy_checks(),
            budget=RunBudget(max_steps=12),
            run_timeout_seconds=20,
            summarizer_factory=lambda _llm: _KeySummarizer("no compaction here"),
        )
    )
    assert report.both_ended_within_budget is True
    assert report.real_summary.compaction_occurred is False
    assert report.valid is False
    assert any(
        "did not invoke the summarizer" in reason for reason in report.invalid_reasons
    )


def test_invalid_when_key_fact_dropped_by_summary(tmp_path: Path) -> None:
    summ = _KeySummarizer("earlier work summarized with no key fact")
    report = asyncio.run(
        compare_case(
            _deploy_spec(),
            llm_factory=_run_factory(lambda: _inspect_program(3)),
            checks=_deploy_checks(),
            budget=RunBudget(max_steps=12),
            run_timeout_seconds=20,
            summarizer_factory=lambda _llm: summ,
        )
    )
    assert report.real_summary.compaction_occurred is True
    assert report.real_summary.key_fact_preserved is False
    assert report.valid is False
    assert any("key tool fact" in reason for reason in report.invalid_reasons)


def test_invalid_when_side_effect_is_replayed(tmp_path: Path) -> None:
    replay = LLMResponse(
        content=None,
        tool_calls=[ToolCall(id="replay", name="apply_side_effect", arguments={})],
    )
    summ = _KeySummarizer("summarized; SEED-DEPLOY-DONE-4471 applied")
    report = asyncio.run(
        compare_case(
            _deploy_spec(),
            llm_factory=_run_factory(lambda: _inspect_program(3, [replay])),
            checks=_deploy_checks(),
            budget=RunBudget(max_steps=12),
            run_timeout_seconds=20,
            summarizer_factory=lambda _llm: summ,
        )
    )
    assert report.real_summary.side_effect_runs == 1
    assert report.real_summary.replayed_side_effect is True
    assert report.valid is False
    assert any("replayed" in reason for reason in report.invalid_reasons)
    # The append-only log now has two entries, so the correctness check fails.
    assert report.real_summary.checks_passed is False


def test_correctness_reported_even_when_other_arm_overspends(tmp_path: Path) -> None:
    # Full arm (budget 8000) is correct; the summary arm's tiny budget makes it
    # exceed the context budget. The full arm's correctness must still be visible.
    report = asyncio.run(
        compare_case(
            _deploy_spec(summary_budget_tokens=200),
            llm_factory=_run_factory(lambda: _inspect_program(3)),
            checks=_deploy_checks(),
            budget=RunBudget(max_steps=12),
            run_timeout_seconds=20,
            summarizer_factory=lambda _llm: _KeySummarizer("x"),
        )
    )
    assert report.full_context.ended_within_budget is True
    assert report.full_context.checks_passed is True
    assert report.real_summary.ended_within_budget is False
    assert report.both_ended_within_budget is False
    # No combined verdict, but the full arm's per-arm correctness is not hidden.
    assert report.final_workspace_correct == {}


def test_reservation_counter_covers_tool_schemas() -> None:
    # The ledger reservation must count tool-schema tokens, or a request could be
    # admitted whose provider payload exceeds the cap. The ContextBuilder keeps a
    # messages-only proxy; the reserve counter must include tools.
    messages = [Message(role="user", content="hello")]
    tools = [{"name": "bash", "description": "run", "parameters": {"type": "object"}}]
    messages_only = _CharCounter().count(messages, tools).tokens
    with_tools = _CharCounter(include_tools=True).count(messages, tools).tokens
    assert with_tools > messages_only

    # A budget sized to fit messages-only but not messages+tools must refuse.
    ledger = TokenBudgetLedger(total=messages_only + 1)
    wrapped = BudgetedLLM(
        _UsageClient(usage=None), ledger, _CharCounter(include_tools=True), output_cap=0
    )
    with pytest.raises(TotalBudgetExceeded):
        asyncio.run(wrapped.generate(messages, tools))
