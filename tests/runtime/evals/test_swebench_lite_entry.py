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


def test_grade_executes_harness_and_summarizes_results(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    results = {
        "resolved": ["astropy__astropy-12907", "django__django-11001"],
        "unresolved": ["sympy__sympy-11400"],
        "empty_patch_ids": [],
        "error_ids": [],
    }
    results_path = tmp_path / "results.json"
    results_path.write_text(json.dumps(results))
    monkeypatch.setattr(script, "_official_harness_available", lambda: True)
    monkeypatch.setattr(
        script, "_run_official_harness", lambda *a, **k: (0, results_path)
    )
    args = _args(
        tmp_path,
        grade=True,
        run_id="r1",
        model="openai/x",
        max_total_tokens=1000,
        isolated=True,
    )
    assert script.main(args) == 0
    result, _ = _parse(capsys)
    assert result["status"] == "GRADED"
    assert result["executed"] is True
    assert result["summary"]["resolved"] == 2
    assert result["summary"]["unresolved"] == 1


def test_grade_propagates_nonzero_harness_exit_as_blocked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(script, "_official_harness_available", lambda: True)
    monkeypatch.setattr(script, "_run_official_harness", lambda *a, **k: (3, None))
    args = _args(
        tmp_path,
        grade=True,
        run_id="r1",
        model="openai/x",
        max_total_tokens=1000,
        isolated=True,
    )
    assert script.main(args) == 1
    result, _ = _parse(capsys)
    assert result["status"] == "LIVE_BLOCKED"
    assert result["executed"] is True  # it ran, but failed


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
