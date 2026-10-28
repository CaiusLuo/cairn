"""The SWE-bench Lite entry is offline by default and gates any real scoring."""

import argparse
import json
from pathlib import Path

from evaluation.benchmarks import swebench_lite as lite
from evaluation.runners import run_swebench_lite as script


def _args(tmp_path: Path, **overrides: object) -> argparse.Namespace:
    base: dict[str, object] = dict(
        check=False,
        diffs=None,
        predictions=tmp_path / "predictions.jsonl",
        model=None,
        live=False,
        official_harness=False,
        max_total_tokens=None,
        run_id=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_check_prints_manifest_without_writing(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    args = _args(tmp_path, check=True)
    assert script.main(args) == 0
    out = capsys.readouterr().out
    assert "swebench-lite-subset-v1" in out
    assert "not the full SWE-bench Lite leaderboard" in out
    assert not (tmp_path / "predictions.jsonl").exists()


def test_adapter_writes_official_jsonl_from_diffs(tmp_path: Path) -> None:
    diffs = tmp_path / "diffs"
    diffs.mkdir()
    (diffs / "astropy__astropy-12907.patch").write_text(
        "diff --git a/x b/x\n", encoding="utf-8"
    )
    args = _args(tmp_path, diffs=diffs, model="cairn-test")
    assert script.main(args) == 0
    lines = (tmp_path / "predictions.jsonl").read_text().splitlines()
    assert len(lines) == len(lite.SUBSET)
    first = json.loads(lines[0])
    assert set(first) == {"instance_id", "model_name_or_path", "model_patch"}
    assert first["model_patch"].startswith("diff --git")
    # An instance with no diff file gets an empty patch (legal, scored empty).
    assert json.loads(lines[1])["model_patch"] == ""


def test_live_without_authorization_is_blocked(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    args = _args(tmp_path, live=True)
    assert script.main(args) == 2
    assert "LIVE_BLOCKED" in capsys.readouterr().err


def test_live_without_official_harness_reports_blocked(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    args = _args(
        tmp_path,
        live=True,
        official_harness=True,
        model="openai/x",
        max_total_tokens=1000,
        run_id="r1",
    )
    assert script.main(args) == 2
    err = capsys.readouterr().err
    assert "LIVE_BLOCKED" in err
    # No scoring happened, so no tests were run by this script.
    assert "No patches were applied" in err


def test_default_mode_is_live_not_run(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    args = _args(tmp_path)
    assert script.main(args) == 0
    assert "LIVE_NOT_RUN" in capsys.readouterr().err
