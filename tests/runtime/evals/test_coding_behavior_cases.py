"""The versioned behavior corpus: no false positives, no false negatives.

This proves the grader is neither too lenient (a near-miss that reproduces the
defect, or a correct target buried in a rewritten file, must fail) nor too strict
(a different-but-correct implementation must pass). Each scenario drives the real
``EvalRunner`` → ``run_turn`` → filesystem tools path with a scripted model, so it
also exercises grader execution isolation and the protected-file checks.
"""

import asyncio
from pathlib import Path

import pytest
from evaluation.cases.coding_smoke_behavior import (
    BEHAVIOR_DATASET_VERSION,
    IDENTITY,
    behavior_cases,
    behavior_dataset_digest,
    behavior_protected_files,
    behavior_suite,
)

from cairn.core.budget import RunBudget
from cairn.core.models import LLMResponse, ToolCall
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.evals import EvalResult, EvalRunner, EvalStatus
from cairn.runtime.evals.behavior import BehaviorCheck
from tests.support.runtime import SequenceLLM
from tests.support.sandbox import require_execution_sandbox

CASES = {spec.case.name: spec for spec in behavior_cases()}
NAMES = tuple(CASES)
SUITE_CHECKS = {item.case.name: item.checks for item in behavior_suite().cases}


def _runner(responses: list[LLMResponse]) -> EvalRunner:
    return EvalRunner(
        lambda: SequenceLLM(responses),
        budget=RunBudget(max_steps=3),
        run_timeout_seconds=15,
        check_timeout_seconds=8,
        trust_local_shell=True,
    )


def _edits(case_name: str, changes: dict[str, str]) -> list[ToolCall]:
    spec = CASES[case_name]
    calls: list[ToolCall] = []
    for i, (path, new_text) in enumerate(changes.items()):
        old = spec.case.files.get(path, "")
        calls.append(
            ToolCall(
                id=f"edit-{i}",
                name="edit_file",
                arguments={"path": path, "old_text": old, "new_text": new_text},
            )
        )
    return calls


def _run(case_name: str, responses: list[LLMResponse]) -> EvalResult:
    spec = CASES[case_name]
    return asyncio.run(
        _runner(responses).run(spec.case, checks=SUITE_CHECKS[case_name])
    )


# --- Reference correct solutions (each differs from the exact-content oracle) --

CORRECT = {
    # Arithmetic variant of the fix; NOT the original byte-exact patch.
    "single-file-bug-fix": {"src/range_utils.py": ""},
    "multi-file-retry-refactor": {
        "src/settings.py": (
            '"""Shared defaults for service helpers."""\n\n'
            "DEFAULT_TIMEOUT_SECONDS = 5\n"
            "DEFAULT_RETRY_LIMIT = 3\n"
        ),
        "src/retry.py": (
            "from .settings import DEFAULT_RETRY_LIMIT\n\n\n"
            "def retry(operation):\n"
            "    for attempt in range(DEFAULT_RETRY_LIMIT):\n"
            "        try:\n"
            "            return operation()\n"
            "        except OSError:\n"
            "            continue\n"
            '    raise RuntimeError("operation failed after retries")\n'
        ),
    },
    "create-and-wire-helper": {
        "src/text_utils.py": (
            "def normalize_username(value: str) -> str:\n"
            "    # A different but correct spelling.\n"
            "    return value.lower().strip()\n"
        ),
        "src/user_service.py": (
            "from src.text_utils import normalize_username\n\n\n"
            "def username_for_lookup(raw: str) -> str:\n"
            "    return normalize_username(raw)\n\n\n"
            "def username_for_storage(raw: str) -> str:\n"
            "    return normalize_username(raw)\n"
        ),
    },
    "minimal-change": {"src/identity.py": ""},
    "no-op-correct-code": {},  # no edits: already correct
}


def _correct_changes(case_name: str) -> dict[str, str]:
    changes = dict(CORRECT[case_name])
    if case_name == "single-file-bug-fix":
        changes["src/range_utils.py"] = (
            CASES[case_name]
            .case.files["src/range_utils.py"]
            .replace("return end - start", "return end - (start - 1)")
        )
    if case_name == "minimal-change":
        changes["src/identity.py"] = (
            CASES[case_name]
            .case.files["src/identity.py"]
            .replace("    return value\n", "    return value.lower().strip()\n", 1)
        )
    return changes


@pytest.mark.parametrize("case_name", NAMES)
def test_initial_buggy_fixture_fails(case_name: str) -> None:
    """The unmodified buggy workspace must fail (no accidental pass)."""
    require_execution_sandbox()
    result = _run(case_name, [LLMResponse(content="done; nothing changed.")])
    expected = EvalStatus.PASS if case_name == "no-op-correct-code" else EvalStatus.FAIL
    assert result.status is expected


@pytest.mark.parametrize("case_name", NAMES)
def test_reference_correct_solution_passes(case_name: str) -> None:
    """A correct solution — even a different spelling — must pass."""
    require_execution_sandbox()
    changes = _correct_changes(case_name)
    calls = _edits(case_name, changes) if changes else []
    responses = ([LLMResponse(tool_calls=calls)] if calls else []) + [
        LLMResponse(content="done")
    ]
    result = _run(case_name, responses)
    assert result.status is EvalStatus.PASS, [
        (c.name, c.message) for c in result.checks if not c.passed
    ]


@pytest.mark.parametrize(
    ("case_name", "changes"),
    [
        # Off-by-one: reproduces the original bug for equal endpoints.
        (
            "single-file-bug-fix",
            {
                "src/range_utils.py": "def inclusive_count(start: int, end: int) -> int:\n"
                '    """Count integers in [start, end], where start <= end."""\n'
                "    return end - start\n"
            },
        ),
        # Fixes the day but keeps a magic literal instead of the constant.
        (
            "multi-file-retry-refactor",
            {
                "src/settings.py": '"""Shared defaults for service helpers."""\n\n'
                "DEFAULT_TIMEOUT_SECONDS = 5\nDEFAULT_RETRY_LIMIT = 3\n",
                "src/retry.py": "def retry(operation):\n"
                "    for attempt in range(3):\n"
                "        try:\n"
                "            return operation()\n"
                "        except OSError:\n"
                "            continue\n"
                '    raise RuntimeError("operation failed after retries")\n',
            },
        ),
        # Extracts a helper but leaves inline normalization in user_service.
        (
            "create-and-wire-helper",
            {
                "src/text_utils.py": "def normalize_username(value: str) -> str:\n"
                "    return value.strip().lower()\n",
            },
        ),
    ],
)
def test_near_miss_solution_fails(case_name: str, changes: dict[str, str]) -> None:
    """A near-miss that reproduces the defect or skips a requirement fails."""
    require_execution_sandbox()
    calls = _edits(case_name, changes)
    result = _run(
        case_name, [LLMResponse(tool_calls=calls), LLMResponse(content="done")]
    )
    assert result.status is EvalStatus.FAIL


def test_protected_unrelated_function_is_rejected() -> None:
    """A correct target fix that breaks an untouched function must fail.

    ``minimal-change`` says to preserve ``format_user_id`` exactly. The behavior
    check asserts that unrelated function still works, so a solution that edits
    the right return expression but also damages ``format_user_id`` is refused.
    """
    require_execution_sandbox()
    broken = IDENTITY.replace(
        '    return f"user-{value:04d}"\n', '    return f"user-{value}"\n'
    ).replace("    return value\n", "    return value.strip().lower()\n", 1)
    calls = _edits("minimal-change", {"src/identity.py": broken})
    result = _run(
        "minimal-change",
        [LLMResponse(tool_calls=calls), LLMResponse(content="done")],
    )
    assert result.status is EvalStatus.FAIL


def test_suite_is_versioned_and_stable() -> None:
    suite = behavior_suite()
    assert suite.name == f"coding-behavior-{BEHAVIOR_DATASET_VERSION}"
    assert [c.case.name for c in suite.cases] == list(NAMES)
    assert behavior_suite() == suite  # deterministic construction
    with pytest.raises(ValueError, match="Unsupported coding behavior"):
        behavior_suite(version="graded-behavior-v1")


def test_versioned_dataset_digest_is_pinned() -> None:
    """A silent prompt/behavior/protected change must require a new version."""
    assert BEHAVIOR_DATASET_VERSION == "graded-behavior-v2"
    assert (
        behavior_dataset_digest()
        == "000f4bbc08059b436b3cd26601fb0dd1c92d3948b85bb72d1967fccca119aa58"
    )


def test_behavior_check_type_is_reported_for_every_case() -> None:
    suite = behavior_suite()
    for item in suite.cases:
        behavior_checks = [c for c in item.checks if c.name.endswith(":behavior")]
        assert len(behavior_checks) == 1
        assert getattr(behavior_checks[0], "check_type", "") == "behavior"


def test_protected_files_pin_unrelated_content() -> None:
    protected = behavior_protected_files()
    # ``None`` entries mean the fix legitimately edits that file, so they are
    # excluded from the byte-identical protected set.
    assert "src/settings.py" not in protected["multi-file-retry-refactor"]
    # Every returned protected value is an exact fixture body that must persist.
    for case_name, files in protected.items():
        for path, contents in files.items():
            assert contents == CASES[case_name].case.files[path]


def test_graded_code_is_executed_inside_throwaway_workspace(
    tmp_path: Path,
) -> None:
    """Grader execution is isolated to a temporary workspace, not the host cwd.

    A behavior script that writes a file must land inside the case workspace.
    The eval runner creates that workspace in a throwaway temp dir, so a write is
    contained there and never appears in the pytest process directory.
    """
    require_execution_sandbox()
    workspace = Workspace(tmp_path)
    probe = (
        "open('probe.txt', 'w', encoding='utf-8').write('x')\n"
        "obs('read', open('probe.txt', encoding='utf-8').read())"
    )
    result = asyncio.run(
        BehaviorCheck("probe", probe, {"read": "x"}).evaluate(workspace)
    )
    assert result.passed is True
    # The write landed in the workspace, not the process working directory.
    assert (tmp_path / "probe.txt").read_text() == "x"
    assert not Path("probe.txt").exists()
