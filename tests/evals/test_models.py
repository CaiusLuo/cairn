import pytest

from cairn.evals.models import CheckResult, EvalResult, EvalStatus


@pytest.mark.parametrize(
    ("status", "check", "trace_id"),
    [
        pytest.param(
            EvalStatus.FAIL,
            CheckResult(name="answer exists", passed=False, message="missing"),
            "trace-fail",
            id="ordinary-failure",
        ),
        pytest.param(
            EvalStatus.ERROR,
            CheckResult(
                name="content readable",
                passed=False,
                error="PermissionError: read denied",
            ),
            "trace-error",
            id="checker-error",
        ),
    ],
)
def test_eval_result_serializes_and_roundtrips_check_outcomes(
    status: EvalStatus, check: CheckResult, trace_id: str
) -> None:
    result = EvalResult(
        case_name="create file",
        status=status,
        checks=[check],
        trace_id=trace_id,
        error=None,
    )

    assert result.model_dump(mode="json") == {
        "case_name": "create file",
        "status": status.value,
        "checks": [
            {
                "name": check.name,
                "passed": False,
                "message": check.message,
                "error": check.error,
            }
        ],
        "trace_id": trace_id,
        "error": None,
    }
    assert EvalResult.model_validate_json(result.model_dump_json()) == result
