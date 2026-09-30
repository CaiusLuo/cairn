import asyncio
from pathlib import Path

import pytest
from examples.evals.coding_smoke_cases import coding_smoke_cases

from cairn.core.budget import RunBudget
from cairn.core.models import LLMResponse, ToolCall
from cairn.evals import (
    EvalCase,
    EvalCheck,
    EvalRunner,
    EvalStatus,
    FileContainsCheck,
    FileContentEqualsCheck,
    FileExistsCheck,
    FileNotContainsCheck,
)
from cairn.workspace.workspace import Workspace
from tests.support.runtime import SequenceLLM

CASES = coding_smoke_cases()
NAMES = tuple(case.name for case, _ in CASES)
FILE_CHECK_TYPES = (
    FileContentEqualsCheck,
    FileContainsCheck,
    FileExistsCheck,
    FileNotContainsCheck,
)
EXPECTED_NAMES = (
    "single-file-bug-fix",
    "multi-file-retry-refactor",
    "create-and-wire-helper",
    "minimal-change",
    "no-op-correct-code",
)


def test_corpus_has_small_safe_builtin_file_checks(tmp_path: Path) -> None:
    assert len(CASES) == 5
    assert NAMES == EXPECTED_NAMES
    assert len(set(NAMES)) == 5
    workspace = Workspace(tmp_path)

    for case, checks in CASES:
        assert case.prompt.strip()
        assert checks
        for raw_path, contents in case.files.items():
            contents.encode("utf-8")
            assert len(contents.splitlines()) < 30
            assert workspace.resolve_path(raw_path).is_relative_to(workspace.root)
        for check in checks:
            assert isinstance(check, FILE_CHECK_TYPES)
            assert workspace.resolve_path(check.path).is_relative_to(workspace.root)

    noop, noop_checks = CASES[-1]
    assert noop.name == "no-op-correct-code"
    assert isinstance(noop_checks[0], FileContentEqualsCheck)
    assert noop_checks[0].expected == noop.files["src/slug.py"]


def _expected_files(case: EvalCase, checks: tuple[EvalCheck, ...]) -> dict[str, str]:
    files = dict(case.files)
    if case.name in {"single-file-bug-fix", "minimal-change"}:
        equality = next(
            check for check in checks if isinstance(check, FileContentEqualsCheck)
        )
        files[equality.path] = equality.expected
    elif case.name == "multi-file-retry-refactor":
        files["src/settings.py"] += "\nDEFAULT_RETRY_LIMIT = 3\n"
        files["src/retry.py"] = (
            "from .settings import (\n"
            "    DEFAULT_RETRY_LIMIT,\n"
            ")\n\n\n"
            + files["src/retry.py"].replace("range(3)", "range(DEFAULT_RETRY_LIMIT)")
        )
    elif case.name == "create-and-wire-helper":
        files["src/text_utils.py"] = (
            "def normalize_username(value: str) -> str:\n"
            "    return value.strip().lower()\n"
        )
        files["src/user_service.py"] = (
            "from src.text_utils import normalize_username\n\n\n"
            + files["src/user_service.py"].replace(
                "raw.strip().lower()", "normalize_username(raw)"
            )
        )
    return files


def _runner(llm: SequenceLLM) -> EvalRunner:
    return EvalRunner(
        lambda: llm,
        budget=RunBudget(max_steps=3),
        run_timeout_seconds=5,
        check_timeout_seconds=1,
    )


@pytest.mark.parametrize(("case", "checks"), CASES, ids=NAMES)
def test_smoke_case_checks_reject_claims_and_accept_real_edits(
    case: EvalCase, checks: tuple[EvalCheck, ...]
) -> None:
    claim = SequenceLLM([LLMResponse(content="Done; the requested work is complete.")])
    claimed_result = asyncio.run(_runner(claim).run(case, checks=checks))
    expected_claim_status = (
        EvalStatus.PASS if case.name == "no-op-correct-code" else EvalStatus.FAIL
    )
    assert claimed_result.status is expected_claim_status
    assert claimed_result.error is None

    expected = _expected_files(case, checks)
    edits = [
        ToolCall(
            id=f"edit-{index}",
            name="edit_file",
            arguments={
                "path": path,
                "old_text": case.files.get(path, ""),
                "new_text": contents,
            },
        )
        for index, (path, contents) in enumerate(expected.items())
        if case.files.get(path) != contents
    ]
    responses = ([LLMResponse(tool_calls=edits)] if edits else []) + [
        LLMResponse(content="Done.")
    ]
    edited_llm = SequenceLLM(responses)
    edited_result = asyncio.run(_runner(edited_llm).run(case, checks=checks))

    assert edited_result.status is EvalStatus.PASS
    assert edited_result.error is None
    assert all(check.passed for check in edited_result.checks)
