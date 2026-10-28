"""BehaviorCheck executes host code and scores behavior, not bytes."""

import asyncio
from pathlib import Path

import pytest

from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.evals.behavior import BehaviorCheck
from cairn.runtime.evals.models import CheckResult
from cairn.runtime.evals.runner import _check_label
from tests.support.sandbox import require_local_shell


def _run(tmp_path: Path, script: str, name: str = "b") -> CheckResult:
    (tmp_path / "src").mkdir(exist_ok=True)
    return asyncio.run(BehaviorCheck(name, script).evaluate(Workspace(tmp_path)))


def test_behavior_passes_for_true_assertion(tmp_path: Path) -> None:
    require_local_shell()
    result = _run(tmp_path, "assert 1 + 1 == 2")
    assert result.passed is True
    assert result.error is None


def test_behavior_fails_with_bounded_diagnostic_on_false_assertion(
    tmp_path: Path,
) -> None:
    require_local_shell()
    result = _run(tmp_path, "assert False, 'nope'")
    assert result.passed is False
    assert "Behavior check failed" in (result.message or "")
    assert len(result.message or "") <= 260


def test_behavior_runs_against_workspace_files(tmp_path: Path) -> None:
    require_local_shell()
    (tmp_path / "value.py").write_text("RESULT = 42\n", encoding="utf-8")
    result = _run(
        tmp_path,
        "import value\nassert value.RESULT == 42",
    )
    assert result.passed is True


def test_behavior_rejects_malformed_definition() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        BehaviorCheck("  ", "assert True")
    with pytest.raises(ValueError, match="non-empty"):
        BehaviorCheck("b", "   ")
    with pytest.raises(ValueError, match="limit"):
        BehaviorCheck("b", "x = 1\n" * 20000)


def test_behavior_strips_declared_secret_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    require_local_shell()
    monkeypatch.setenv("CAIRN_TEST_SECRET", "leaked-value")
    script = "import os\nassert 'CAIRN_TEST_SECRET' not in os.environ"
    result = asyncio.run(
        BehaviorCheck(
            "b", script, secret_env_keys=frozenset({"CAIRN_TEST_SECRET"})
        ).evaluate(Workspace(tmp_path))
    )
    assert result.passed is True


def test_behavior_check_reports_stable_type_label() -> None:
    check = BehaviorCheck("c", "assert True")
    # Reporting uses the declared check_type, not the concrete class name.
    assert _check_label(check) == "behavior"


def test_behavior_check_protocol_type_is_classvar_string() -> None:
    assert BehaviorCheck.check_type == "behavior"
    assert isinstance(BehaviorCheck.check_type, str)
