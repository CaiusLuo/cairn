"""Host answers exist only in offline harness tests, never evaluated prompts."""

import asyncio
from pathlib import Path

import pytest

from cairn.core.budget import RunBudget
from cairn.core.models import LLMResponse, ToolCall
from cairn.runtime.evals.intelligence.dataset import (
    V1_VERSION,
    VERSION,
    dataset,
    dataset_digest,
)
from cairn.runtime.evals.intelligence.models import Arm, IntelligenceMetrics
from cairn.runtime.evals.intelligence.runner import IntelligenceRunner
from cairn.runtime.evals.models import EvalStatus
from tests.support.runtime import SequenceLLM

# Independent reference implementations check that the frozen outcome checks
# admit legitimate solutions. Fake scripted edits do not measure model quality.
REFERENCE_PATCHES = {
    "quote-dependencies": {
        "forge/pricing/calculator.py": "def catalog_subtotal(rows):\n    return round(sum(p * q for p, q in rows), 2)\n",
        "forge/checkout/service.py": "from forge.pricing.calculator import catalog_subtotal\nfrom forge.shipping.policy import delivery_fee\n\ndef checkout_total(rows, expedited=False):\n    subtotal = catalog_subtotal(rows)\n    return round(subtotal * 1.08 + delivery_fee(subtotal, expedited), 2)\n",
    },
    "find-label-normalizer": {
        "forge/text/slugs.py": "import re\n\ndef canonical_label(value):\n    return re.sub(r'[ \\t\\n\\r\\f\\v]+', '-', value.strip(' \\t\\n\\r\\f\\v')).casefold()\n",
    },
    "shared-account-contract": {
        "forge/cache/keys.py": "from forge.storage.identities import normalize_account\n\ndef cache_identity(raw):\n    return 'account:' + normalize_account(raw)\n",
        "forge/audit/records.py": "from forge.storage.identities import normalize_account\n\ndef audit_identity(raw):\n    return {'account': normalize_account(raw)}\n",
    },
    "testing-convention": {
        "forge/storage/identities.py": "def normalize_account(raw):\n    value = raw.strip().casefold()\n    if not value:\n        raise ValueError('blank')\n    return value\n",
        "checks/check_account.py": "import unittest\nfrom forge.storage.identities import normalize_account\n\nclass AccountCase(unittest.TestCase):\n    def test_existing(self):\n        self.assertEqual(normalize_account(' A '), 'a')\n    def test_blank(self):\n        self.assertRaises(ValueError, normalize_account, ' \\t ')\n",
    },
    "stale-batch-history": {
        "forge/routing/chunks.py": "from forge.routing.limits import MAX_BATCH\n\ndef split_batches(values):\n    return [values[i:i + MAX_BATCH] for i in range(0, len(values), MAX_BATCH)]\n",
    },
    "unrelated-memory-control": {
        "forge/telemetry/reduce.py": "def median_delay(samples):\n    values = sorted(samples)\n    n = len(values)\n    if not n:\n        return 0\n    if n % 2:\n        return values[n // 2]\n    return (values[n // 2 - 1] + values[n // 2]) / 2\n",
    },
}


def test_dataset_contract_and_no_prompt_answers() -> None:
    cases = dataset()
    assert VERSION == "repo-intelligence-v2" and len(cases) == 6
    assert dataset_digest(cases) == dataset_digest(dataset())
    assert (
        dataset_digest(cases)
        == "12dfcc1d70c28cfd3a670b8bfe85a0f3d766c370416d75f3072bbe11e2426cdf"
    )
    assert len({spec.item.case.name for spec in cases}) == 6
    for spec in cases:
        for path in spec.relevant_files:
            assert path not in spec.item.case.prompt
        assert len(spec.item.case.files) >= 30
        for fact in spec.memories:
            assert fact.evidence_files and len(fact.statement) < 512
            assert all(path in spec.item.case.files for path in fact.evidence_files)
            assert "def " not in fact.statement and "return " not in fact.statement


def test_v1_dataset_contract_remains_frozen() -> None:
    historical = dataset(version=V1_VERSION)
    corrected = dataset()
    assert V1_VERSION == "repo-intelligence-v1"
    assert (
        dataset_digest(historical)
        == "b459ce3fd1293e540572a2ebf8ae0c4a10407fd28a4be76f117a776d45e7553f"
    )
    for old, new in zip(historical, corrected, strict=True):
        assert old.item.case == new.item.case
        assert old.memories == new.memories
        assert old.relevant_files == new.relevant_files
        assert old.current_files == new.current_files
    with pytest.raises(ValueError, match="Unsupported repository intelligence"):
        dataset(version="repo-intelligence-v3")


@pytest.mark.parametrize("case_index", range(6))
@pytest.mark.parametrize("fixed", [False, True])
def test_independent_outcomes_accept_solutions_and_reject_initial_bugs(
    tmp_path: Path, case_index: int, fixed: bool
) -> None:
    spec = dataset()[case_index]
    responses = []
    if fixed:
        responses.append(
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id=f"edit-{i}",
                        name="edit_file",
                        arguments={
                            "path": path,
                            "old_text": spec.item.case.files[path],
                            "new_text": text,
                        },
                    )
                    for i, (path, text) in enumerate(
                        REFERENCE_PATCHES[spec.item.case.name].items()
                    )
                ]
            )
        )
    responses.append(LLMResponse(content="done"))
    runner = IntelligenceRunner(
        lambda: SequenceLLM(responses),
        spec=spec,
        arm=Arm.BASELINE,
        observations=IntelligenceMetrics(),
        budget=RunBudget(max_steps=3),
        run_timeout_seconds=15,
        check_timeout_seconds=8,
        temp_root=tmp_path,
    )
    result = asyncio.run(runner.run_case())
    assert result.status is (EvalStatus.PASS if fixed else EvalStatus.FAIL)
    assert [c.name for c in result.checks if not c.passed] == (
        [] if fixed else [f"{spec.item.case.name}:behavior"]
    )


def test_testing_convention_requires_executable_regression(tmp_path: Path) -> None:
    spec = dataset()[3]
    path = "forge/storage/identities.py"
    responses = [
        LLMResponse(
            tool_calls=[
                ToolCall(
                    id="fix",
                    name="edit_file",
                    arguments={
                        "path": path,
                        "old_text": spec.item.case.files[path],
                        "new_text": REFERENCE_PATCHES[spec.item.case.name][path],
                    },
                )
            ]
        ),
        LLMResponse(content="done"),
    ]
    runner = IntelligenceRunner(
        lambda: SequenceLLM(responses),
        spec=spec,
        arm=Arm.BASELINE,
        observations=IntelligenceMetrics(),
        budget=RunBudget(max_steps=3),
        run_timeout_seconds=15,
        check_timeout_seconds=8,
        temp_root=tmp_path,
    )
    assert asyncio.run(runner.run_case()).status is EvalStatus.FAIL


def test_unrelated_case_rejects_corrupted_account_check(tmp_path: Path) -> None:
    spec = dataset()[5]
    fixes = REFERENCE_PATCHES[spec.item.case.name]
    account_check = "checks/check_account.py"
    responses = [
        LLMResponse(
            tool_calls=[
                *(
                    ToolCall(
                        id=f"fix-{i}",
                        name="edit_file",
                        arguments={
                            "path": path,
                            "old_text": spec.item.case.files[path],
                            "new_text": text,
                        },
                    )
                    for i, (path, text) in enumerate(fixes.items())
                ),
                ToolCall(
                    id="corrupt-account-check",
                    name="edit_file",
                    arguments={
                        "path": account_check,
                        "old_text": spec.item.case.files[account_check],
                        "new_text": "corrupted\n",
                    },
                ),
            ]
        ),
        LLMResponse(content="done"),
    ]
    runner = IntelligenceRunner(
        lambda: SequenceLLM(responses),
        spec=spec,
        arm=Arm.BASELINE,
        observations=IntelligenceMetrics(),
        budget=RunBudget(max_steps=3),
        run_timeout_seconds=15,
        check_timeout_seconds=8,
        temp_root=tmp_path,
    )
    result = asyncio.run(runner.run_case())
    assert result.status is EvalStatus.FAIL
    assert [check.name for check in result.checks if not check.passed] == [
        "preserve:checks/check_account.py"
    ]


def test_negative_control_retrieves_no_memory(tmp_path: Path) -> None:
    llm = SequenceLLM([LLMResponse(content="done")])
    runner = IntelligenceRunner(
        lambda: llm,
        spec=dataset()[5],
        arm=Arm.MEMORY,
        observations=IntelligenceMetrics(),
        budget=RunBudget(max_steps=2),
        run_timeout_seconds=15,
        check_timeout_seconds=8,
        temp_root=tmp_path,
    )
    assert asyncio.run(runner.run_case()).status is EvalStatus.FAIL
    assert runner.observations.memory_fixture_entries == 1
    assert runner.observations.memory_selected_events == 0
    assert runner.observations.relevant_memory_precision is None
    assert not any('"project_memory"' in (m.content or "") for m in llm.calls[0][0])
