"""BehaviorCheck: host-side verdict from bounded observations, real sandbox.

Covers the untrusted-candidate attacks A demands: a candidate that calls
``os._exit(0)``, ``sys.exit(0)``, prints a fake SUCCESS line, or crashes must never
be marked PASS; correct vs wrong implementations are distinguished; and the
sandbox keeps host secrets/network/markers out of reach.
"""

import asyncio
from pathlib import Path

import pytest

import cairn.infra.sandbox as sandbox
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.evals.behavior import BehaviorCheck, Contains
from cairn.runtime.evals.models import CheckResult
from cairn.runtime.evals.runner import _check_label
from tests.support.sandbox import require_execution_sandbox


def _run(
    tmp_path: Path, program: str, expected: dict[str, object], name: str = "b"
) -> CheckResult:
    (tmp_path / "src").mkdir(exist_ok=True)
    return asyncio.run(
        BehaviorCheck(name, program, expected).evaluate(Workspace(tmp_path))
    )


def test_passes_when_observations_match(tmp_path: Path) -> None:
    require_execution_sandbox()
    result = _run(tmp_path, "obs('x', 1 + 1)", {"x": 2})
    assert result.passed is True and result.error is None


def test_fails_on_wrong_value(tmp_path: Path) -> None:
    require_execution_sandbox()
    result = _run(tmp_path, "obs('x', 1 + 1)", {"x": 3})
    assert result.passed is False
    assert "expected" in (result.message or "")


def test_fails_on_missing_observation(tmp_path: Path) -> None:
    require_execution_sandbox()
    result = _run(tmp_path, "obs('x', 1)", {"x": 1, "y": 2})
    assert result.passed is False
    assert "missing observation" in (result.message or "")


def test_contains_expectation(tmp_path: Path) -> None:
    require_execution_sandbox()
    ok = _run(tmp_path, "obs('doc', 'start here')", {"doc": Contains("start")})
    bad = _run(tmp_path, "obs('doc', 'nope')", {"doc": Contains("start")})
    assert ok.passed is True
    assert bad.passed is False


def test_rejects_malformed_definition() -> None:
    with pytest.raises(ValueError, match="name"):
        BehaviorCheck("  ", "obs('x',1)", {"x": 1})
    with pytest.raises(ValueError, match="program"):
        BehaviorCheck("b", "   ", {"x": 1})
    with pytest.raises(ValueError, match="expected"):
        BehaviorCheck("b", "obs('x',1)", {})


def test_check_type_label() -> None:
    check = BehaviorCheck("c", "obs('x',1)", {"x": 1})
    assert _check_label(check) == "behavior"
    assert BehaviorCheck.check_type == "behavior"


def _candidate(tmp_path: Path, source: str) -> None:
    (tmp_path / "candidate.py").write_text(source, encoding="utf-8")


def test_candidate_os_exit_cannot_fake_pass(tmp_path: Path) -> None:
    require_execution_sandbox()
    _candidate(tmp_path, "import os\nos._exit(0)\n")
    result = _run(tmp_path, "import candidate\nobs('ran', True)\n", {"ran": True})
    assert result.passed is False
    assert "no observation record" in (result.message or "")


def test_candidate_sys_exit_cannot_fake_pass(tmp_path: Path) -> None:
    require_execution_sandbox()
    _candidate(tmp_path, "import sys\nsys.exit(0)\n")
    result = _run(tmp_path, "import candidate\nobs('ran', True)\n", {"ran": True})
    assert result.passed is False
    assert "did not complete" in (result.message or "")


def test_candidate_fake_success_output_is_ignored(tmp_path: Path) -> None:
    require_execution_sandbox()
    _candidate(
        tmp_path,
        "import os, sys\nprint('SUCCESS: all checks passed')\nsys.stdout.flush()\nos._exit(0)\n",
    )
    result = _run(tmp_path, "import candidate\nobs('ran', True)\n", {"ran": True})
    assert result.passed is False
    assert "SUCCESS" not in (result.message or "")


def test_candidate_raising_cannot_fake_pass(tmp_path: Path) -> None:
    require_execution_sandbox()
    _candidate(tmp_path, "raise RuntimeError('boom')\n")
    result = _run(tmp_path, "import candidate\nobs('ran', True)\n", {"ran": True})
    assert result.passed is False
    assert "did not complete" in (result.message or "")


def test_correct_vs_wrong_implementation(tmp_path: Path) -> None:
    require_execution_sandbox()
    (tmp_path / "src").mkdir(exist_ok=True)
    program = (
        "import importlib.util\n"
        "spec = importlib.util.spec_from_file_location('impl', 'src/impl.py')\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "obs('add', m.add(2, 3))\n"
    )
    (tmp_path / "src" / "impl.py").write_text("def add(a, b):\n    return a + b\n")
    good = _run(tmp_path, program, {"add": 5})
    (tmp_path / "src" / "impl.py").write_text("def add(a, b):\n    return a - b\n")
    bad = _run(tmp_path, program, {"add": 5})
    assert good.passed is True
    assert bad.passed is False


def test_etc_tree_is_not_visible(tmp_path: Path) -> None:
    require_execution_sandbox()
    program = (
        "import os\n"
        "obs('passwd', os.path.exists('/etc/passwd'))\n"
        "obs('shadow', os.path.exists('/etc/shadow'))\n"
    )
    result = _run(tmp_path, program, {"passwd": False, "shadow": False})
    assert result.passed is True, result.message


def test_injected_secret_env_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    require_execution_sandbox()
    monkeypatch.setenv("CAIRN_TEST_SECRET", "leaked")
    result = _run(
        tmp_path,
        "import os\nobs('present', 'CAIRN_TEST_SECRET' in os.environ)",
        {"present": False},
    )
    assert result.passed is True


def test_network_blocked(tmp_path: Path) -> None:
    require_execution_sandbox()
    program = (
        "try:\n"
        "    import urllib.request; urllib.request.urlopen('http://1.1.1.1', timeout=2)\n"
        "    obs('net', 'reachable')\n"
        "except Exception:\n"
        "    obs('net', 'blocked')\n"
    )
    assert _run(tmp_path, program, {"net": "blocked"}).passed is True


def test_workspace_symlink_cannot_escape(tmp_path: Path) -> None:
    require_execution_sandbox()
    host_secret = tmp_path.parent / "outside_secret.txt"
    host_secret.write_text("TOP_SECRET_OUTSIDE", encoding="utf-8")
    (tmp_path / "evil").symlink_to(host_secret)
    program = (
        "try:\n"
        "    obs('escape', open('evil').read())\n"
        "except (FileNotFoundError, OSError):\n"
        "    obs('escape', 'blocked')\n"
    )
    assert _run(tmp_path, program, {"escape": "blocked"}).passed is True


def test_resource_limits_applied(tmp_path: Path) -> None:
    require_execution_sandbox()
    program = (
        "import resource\n"
        "obs('as_positive', resource.getrlimit(resource.RLIMIT_AS)[0] > 0)\n"
        "obs('core_zero', resource.getrlimit(resource.RLIMIT_CORE)[0] == 0)\n"
    )
    assert (
        _run(tmp_path, program, {"as_positive": True, "core_zero": True}).passed is True
    )


def test_missing_sandbox_fails_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = tmp_path / "executed.txt"
    program = f"obs('x', open({str(sentinel)!r}, 'w').write('ran'))"
    monkeypatch.setattr(sandbox, "available", lambda: False)
    result = asyncio.run(
        BehaviorCheck("b", program, {"x": 3}).evaluate(Workspace(tmp_path))
    )
    assert (
        result.passed is False
        and result.error
        and "IsolationUnavailable" in result.error
    )
    assert not sentinel.exists()


def test_sandbox_bind_set_excludes_etc_and_sbin() -> None:
    assert set(sandbox._RO_BINDS) == {"/usr", "/lib", "/lib64", "/bin"}


def test_sandbox_rejects_bare_path() -> None:
    from cairn.infra.sandbox import build_argv

    with pytest.raises(TypeError, match="Workspace"):
        build_argv(Path("/tmp"), "assert True", sandbox.IsolationLimits())  # type: ignore[arg-type]
