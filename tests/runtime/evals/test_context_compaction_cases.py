"""The fixed compaction cases, run as deterministic offline checks.

These are Cairn's own contract, not a model benchmark: every input is scripted,
so a failure means compaction broke and never that a provider behaved oddly. Real
model effect is measured separately and must not be added here as an assertion.
"""

from typing import Any

import pytest
from evaluation.cases.context_compaction import (
    CaseFacts,
    offline_compaction_cases,
    tool_facts,
)
from evaluation.runners.run_context_compaction import run_all, run_case

CASES = offline_compaction_cases()


def case_ids() -> list[str]:
    return [case.name for case in CASES]


@pytest.mark.parametrize("case", CASES, ids=case_ids())
def test_compaction_case_preserves_its_facts(case: Any) -> None:
    result = run_case(case)

    assert result.passed, "\n".join(result.failures)


@pytest.mark.parametrize("case", CASES, ids=case_ids())
def test_compaction_never_replays_a_tool(case: Any) -> None:
    facts: CaseFacts = run_case(case).facts

    assert facts.tool_run_count == case.expected_tool_runs


@pytest.mark.parametrize("case", CASES, ids=case_ids())
def test_current_instruction_survives_verbatim(case: Any) -> None:
    facts = run_case(case).facts

    instructions = [
        message.content for message in facts.conversation if message.role == "user"
    ]
    assert case.current_instruction in instructions


@pytest.mark.parametrize("case", CASES, ids=case_ids())
def test_recorded_tool_facts_keep_their_content(case: Any) -> None:
    facts = run_case(case).facts

    for message in tool_facts(facts.conversation):
        assert message.content, "compaction must not erase an executed tool fact"


def test_every_case_finishes_its_turn() -> None:
    results = run_all()

    unfinished = [
        result.name for result in results if result.facts.answer != "finished"
    ]
    assert not unfinished, f"turns ended without an answer: {unfinished}"


def test_at_least_one_case_exercises_the_summarizer() -> None:
    """Rule folding alone would leave the model-summary path untested."""
    results = run_all()

    assert any(result.facts.summary_calls > 0 for result in results), (
        "no case reached the summarizer; the model-summary path has no coverage"
    )
