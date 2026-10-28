"""BehaviorCheck runs generated code in a real sandbox and scores behavior.

The check executes the host-owned script (together with the model-generated code
it imports) inside bubblewrap, so these tests both verify behavior grading and
prove the isolation boundary: host files and secrets stay out of reach, and a
missing sandbox fails explicitly instead of running code on the host.
"""

import asyncio
from pathlib import Path

import pytest

import cairn.infra.sandbox as sandbox
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.evals.behavior import BehaviorCheck
from cairn.runtime.evals.models import CheckResult
from cairn.runtime.evals.runner import _check_label
from tests.support.sandbox import require_execution_sandbox


def _run(tmp_path: Path, script: str, name: str = "b") -> CheckResult:
    (tmp_path / "src").mkdir(exist_ok=True)
    return asyncio.run(BehaviorCheck(name, script).evaluate(Workspace(tmp_path)))


def test_behavior_passes_for_true_assertion(tmp_path: Path) -> None:
    require_execution_sandbox()
    result = _run(tmp_path, "assert 1 + 1 == 2")
    assert result.passed is True
    assert result.error is None


def test_behavior_fails_with_bounded_diagnostic_on_false_assertion(
    tmp_path: Path,
) -> None:
    require_execution_sandbox()
    result = _run(tmp_path, "assert False, 'nope'")
    assert result.passed is False
    assert "Behavior check failed" in (result.message or "")
    assert len(result.message or "") <= 260


def test_behavior_runs_against_workspace_files(tmp_path: Path) -> None:
    require_execution_sandbox()
    (tmp_path / "value.py").write_text("RESULT = 42\n", encoding="utf-8")
    result = _run(tmp_path, "import value\nassert value.RESULT == 42")
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
    require_execution_sandbox()
    monkeypatch.setenv("CAIRN_TEST_SECRET", "leaked-value")
    script = "import os\nassert 'CAIRN_TEST_SECRET' not in os.environ"
    result = asyncio.run(BehaviorCheck("b", script).evaluate(Workspace(tmp_path)))
    assert result.passed is True


def test_behavior_check_reports_stable_type_label() -> None:
    check = BehaviorCheck("c", "assert True")
    assert _check_label(check) == "behavior"


def test_behavior_check_protocol_type_is_classvar_string() -> None:
    assert BehaviorCheck.check_type == "behavior"
    assert isinstance(BehaviorCheck.check_type, str)


# --- Isolation boundary proofs ------------------------------------------------


def test_generated_code_cannot_read_host_secret_or_write_host_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Generated code cannot reach a host secret file or overwrite a marker."""
    require_execution_sandbox()
    monkeypatch.setenv("CAIRN_HOST_SECRET", "TOP_SECRET_VALUE")
    host_secret = tmp_path.parent / "host_secret.txt"
    host_marker = tmp_path.parent / "host_marker.txt"
    host_secret.write_text("TOP_SECRET_VALUE", encoding="utf-8")
    host_marker.write_text("untouched", encoding="utf-8")
    # Script: it may exist only inside the workspace; it must not see the host
    # secret path, the env var, or be able to overwrite the host marker.
    script = (
        f"import os\n"
        f"assert 'CAIRN_HOST_SECRET' not in os.environ\n"
        f"assert not os.path.exists({str(host_secret)!r})\n"
        f"assert not os.path.exists({str(host_marker)!r})\n"
        f"print('isolated')\n"
    )
    result = _run(tmp_path, script)
    assert result.passed is True, result.message
    # The host marker is unchanged after the run.
    assert host_marker.read_text(encoding="utf-8") == "untouched"


def test_generated_code_has_no_network(tmp_path: Path) -> None:
    require_execution_sandbox()
    script = (
        "import urllib.request\n"
        "try:\n"
        "    urllib.request.urlopen('http://1.1.1.1', timeout=2)\n"
        "except Exception:\n"
        "    print('blocked')\n"
        "else:\n"
        "    raise SystemExit('network was reachable')\n"
    )
    result = _run(tmp_path, script)
    assert result.passed is True, result.message


def test_missing_isolation_fails_explicitly_and_never_runs_on_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No sandbox => an explicit error, and the code never executes."""
    sentinel = tmp_path / "executed.txt"
    script = f"open({str(sentinel)!r}, 'w').write('ran')"
    monkeypatch.setattr(sandbox, "available", lambda: False)
    result = asyncio.run(BehaviorCheck("b", script).evaluate(Workspace(tmp_path)))
    assert result.passed is False
    assert result.error is not None
    assert "IsolationUnavailable" in result.error
    # The host sentinel proves the generated code did not run.
    assert not sentinel.exists()


def test_resource_limits_are_applied(tmp_path: Path) -> None:
    require_execution_sandbox()
    script = (
        "import resource\n"
        "as_limit = resource.getrlimit(resource.RLIMIT_AS)[0]\n"
        "cpu = resource.getrlimit(resource.RLIMIT_CPU)[0]\n"
        "core = resource.getrlimit(resource.RLIMIT_CORE)[0]\n"
        "assert as_limit > 0, as_limit\n"
        "assert cpu > 0, cpu\n"
        "assert core == 0, core\n"
    )
    result = _run(tmp_path, script)
    assert result.passed is True, result.message
