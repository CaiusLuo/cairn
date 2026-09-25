import pytest
from pydantic import ValidationError

from cairn.evals.models import CheckResult, EvalCase, EvalResult, EvalStatus


def test_eval_case_requires_a_prompt() -> None:
    case = EvalCase(name="create file", prompt="Create answer.txt")

    assert case.model_dump() == {
        "name": "create file",
        "prompt": "Create answer.txt",
    }
    with pytest.raises(ValidationError):
        EvalCase.model_validate({"name": "missing prompt"})


def test_eval_result_serializes_check_results_and_status() -> None:
    result = EvalResult(
        case_name="create file",
        status=EvalStatus.FAIL,
        checks=[CheckResult(name="answer exists", passed=False, message="missing")],
        trace_id="trace-1",
    )

    assert result.model_dump(mode="json") == {
        "case_name": "create file",
        "status": "fail",
        "checks": [{"name": "answer exists", "passed": False, "message": "missing"}],
        "trace_id": "trace-1",
        "error": None,
    }
    assert EvalResult.model_validate_json(result.model_dump_json()) == result


def test_eval_result_checks_default_is_independent() -> None:
    first = EvalResult(case_name="first", status=EvalStatus.PASS)
    second = EvalResult(case_name="second", status=EvalStatus.PASS)

    first.checks.append(CheckResult(name="file exists", passed=True))

    assert second.checks == []


def test_eval_result_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        EvalResult.model_validate({"case_name": "example", "status": "skipped"})
