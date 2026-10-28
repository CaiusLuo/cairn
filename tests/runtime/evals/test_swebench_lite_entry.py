"""SWE-bench Lite entry: distinct modes never pass a printed command off as a run."""

import argparse
import json
from pathlib import Path
from typing import Any

import pytest
from evaluation.benchmarks import swebench_lite as lite
from evaluation.runners import run_swebench_lite as script


def _args(tmp_path: Path, **overrides: object) -> argparse.Namespace:
    base: dict[str, object] = dict(
        check=False,
        diffs=None,
        predictions=tmp_path / "predictions.jsonl",
        print_command=False,
        grade=False,
        solve=False,
        tasks=None,
        max_steps=10,
        solve_timeout_seconds=30.0,
        live=False,
        official_harness=False,
        model=None,
        max_total_tokens=None,
        run_id=None,
        isolated=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _parse(capsys: pytest.CaptureFixture[str]) -> tuple[dict[str, Any], str]:
    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if line.startswith("RESULT "))
    return json.loads(line[len("RESULT ") :]), out


def test_check_reports_manifest_ok_without_executing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert script.main(_args(tmp_path, check=True)) == 0
    result, _ = _parse(capsys)
    assert result["status"] == "MANIFEST_OK"
    assert result["executed"] is False


def test_diffs_mode_converts_predictions_without_scoring(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    diffs = tmp_path / "diffs"
    diffs.mkdir()
    (diffs / "astropy__astropy-12907.patch").write_text("diff --git a/x b/x\n")
    args = _args(tmp_path, diffs=diffs, model="cairn-test")
    assert script.main(args) == 0
    result, _ = _parse(capsys)
    assert result["status"] == "PREDICTIONS_WRITTEN"
    assert result["executed"] is False
    lines = (tmp_path / "predictions.jsonl").read_text().splitlines()
    assert len(lines) == len(lite.SUBSET)
    assert json.loads(lines[0])["model_patch"].startswith("diff --git")


def test_print_command_mode_does_not_execute(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _args(tmp_path, print_command=True, run_id="r1")
    assert script.main(args) == 0
    result, out = _parse(capsys)
    assert result["status"] == "COMMAND_READY"
    assert result["executed"] is False
    assert "swebench.harness.run_evaluation" in out


def test_grade_requires_model_budget_isolation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _args(tmp_path, grade=True, run_id="r1")  # missing model/budget/isolated
    assert script.main(args) == 2
    result, _ = _parse(capsys)
    assert result["status"] == "LIVE_BLOCKED"
    assert result["executed"] is False
    assert set(result["missing"]) == {"--model", "--max-total-tokens", "--isolated"}


def test_grade_without_official_harness_is_blocked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(script, "_docker_available", lambda: True)
    monkeypatch.setattr(script, "_official_harness_available", lambda: False)
    args = _args(
        tmp_path,
        grade=True,
        run_id="r1",
        model="openai/x",
        max_total_tokens=1000,
        isolated=True,
    )
    assert script.main(args) == 2
    result, _ = _parse(capsys)
    assert result["status"] == "LIVE_BLOCKED"
    assert result["executed"] is False
    assert "official-harness" in result["missing"]


def _official_report(subset_ids: list[str]) -> dict[str, object]:
    return {
        "total_instances": 300,
        "submitted_ids": subset_ids,
        "resolved_ids": subset_ids[:2],
        "unresolved_ids": subset_ids[2:3],
        "empty_patch_ids": [],
        "error_ids": [],
    }


def _grade_args(tmp_path: Path) -> argparse.Namespace:
    return _args(
        tmp_path,
        grade=True,
        run_id="r1",
        model="openai/x",
        max_total_tokens=1000,
        isolated=True,
    )


def _fake_harness_writing(payload: object, *, run_id: str = "r1", age: float = 0.0):  # type: ignore[no-untyped-def]
    def run(predictions, requested_run_id, **kwargs):  # type: ignore[no-untyped-def]
        path = Path("logs") / "evaluation" / requested_run_id / "model" / "results.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
        if age:
            import os
            import time as _t

            old = _t.time() - age
            os.utime(path, (old, old))
        return 0, path

    return run


def test_grade_executes_and_validates_official_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    ids = lite.instance_ids()
    monkeypatch.setattr(script, "_docker_available", lambda: True)
    monkeypatch.setattr(script, "_official_harness_available", lambda: True)
    monkeypatch.setattr(
        script,
        "_run_official_harness",
        _fake_harness_writing(_official_report(list(ids[:3]))),
    )
    assert script.main(_grade_args(tmp_path)) == 0
    result, _ = _parse(capsys)
    assert result["status"] == "GRADED"
    assert result["executed"] is True
    assert result["summary"]["resolved"] == 2
    assert result["summary"]["unresolved"] == 1
    assert result["error_class"] is None


def test_grade_exit0_but_missing_results_is_blocked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(script, "_docker_available", lambda: True)
    monkeypatch.setattr(script, "_official_harness_available", lambda: True)
    monkeypatch.setattr(script, "_run_official_harness", lambda *a, **k: (0, None))
    assert script.main(_grade_args(tmp_path)) == 1
    result, _ = _parse(capsys)
    assert result["status"] == "LIVE_BLOCKED"
    assert result["error_class"] == "missing_results"


def test_grade_bad_json_is_parse_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(script, "_docker_available", lambda: True)
    monkeypatch.setattr(script, "_official_harness_available", lambda: True)
    monkeypatch.setattr(
        script, "_run_official_harness", _fake_harness_writing("{not json")
    )
    assert script.main(_grade_args(tmp_path)) == 1
    result, _ = _parse(capsys)
    assert result["status"] == "LIVE_BLOCKED"
    assert result["error_class"] == "parse_error"


def test_grade_stale_results_is_blocked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(script, "_docker_available", lambda: True)
    monkeypatch.setattr(script, "_official_harness_available", lambda: True)
    monkeypatch.setattr(
        script,
        "_run_official_harness",
        _fake_harness_writing(
            _official_report(list(lite.instance_ids()[:3])), age=3600
        ),
    )
    assert script.main(_grade_args(tmp_path)) == 1
    result, _ = _parse(capsys)
    assert result["status"] == "LIVE_BLOCKED"
    assert result["error_class"] == "stale_results"


def test_grade_mismatched_instances_is_blocked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(script, "_docker_available", lambda: True)
    monkeypatch.setattr(script, "_official_harness_available", lambda: True)
    monkeypatch.setattr(
        script,
        "_run_official_harness",
        _fake_harness_writing(
            _official_report(["not__in-subset-1", "not__in-subset-2"])
        ),
    )
    assert script.main(_grade_args(tmp_path)) == 1
    result, _ = _parse(capsys)
    assert result["status"] == "LIVE_BLOCKED"
    assert result["error_class"] == "instance_mismatch"


def test_grade_nonzero_exit_is_execution_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(script, "_docker_available", lambda: True)
    monkeypatch.setattr(script, "_official_harness_available", lambda: True)
    monkeypatch.setattr(script, "_run_official_harness", lambda *a, **k: (3, None))
    assert script.main(_grade_args(tmp_path)) == 1
    result, _ = _parse(capsys)
    assert result["status"] == "LIVE_BLOCKED"
    assert result["executed"] is True
    assert result["error_class"] == "execution"


def test_live_reports_partial_closed_loop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _args(tmp_path, live=True, predictions=None)
    assert script.main(args) == 2
    result, _ = _parse(capsys)
    assert result["status"] == "PARTIAL"
    assert result["executed"] is False
    assert "cairn-solve-patch-generation" in result["missing"][0]


def test_default_is_live_not_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _args(tmp_path, predictions=None)
    assert script.main(args) == 0
    result, _ = _parse(capsys)
    assert result["status"] == "LIVE_NOT_RUN"
    assert result["executed"] is False


def test_grade_requires_real_docker_evidence_not_self_declaration(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(script, "_docker_available", lambda: False)
    args = _args(
        tmp_path,
        grade=True,
        run_id="r1",
        model="openai/x",
        max_total_tokens=1000,
        isolated=True,
    )
    assert script.main(args) == 2
    result, _ = _parse(capsys)
    assert result["status"] == "LIVE_BLOCKED"
    assert result["executed"] is False
    assert "docker-daemon" in result["missing"]
