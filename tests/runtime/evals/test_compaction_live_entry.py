"""The live compaction entry must never spend unless explicitly authorized."""

import argparse
import json
from pathlib import Path

from evaluation.runners import run_compaction_live as script


def _args(tmp_path: Path, **overrides: object) -> argparse.Namespace:
    base = dict(
        report=tmp_path / "report.json",
        live=False,
        model=None,
        max_total_tokens=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_default_is_live_not_run_and_spends_nothing(tmp_path: Path) -> None:
    args = _args(tmp_path)
    assert script.main(args) == 0
    payload = json.loads((tmp_path / "report.json").read_text())
    assert payload["status"] == "LIVE_NOT_RUN"
    # No arm ran, so all observations are null, not fabricated.
    assert payload["full_context"] is None
    assert payload["real_summary"] is None
    assert payload["model_identifier"] is None
    assert payload["both_ended_within_budget"] is None


def test_live_without_model_or_budget_is_refused(tmp_path: Path) -> None:
    args = _args(tmp_path, live=True, model="openai/x", max_total_tokens=None)
    assert script.main(args) == 2
    assert not (tmp_path / "report.json").exists()


def test_live_without_model_is_refused(tmp_path: Path) -> None:
    args = _args(tmp_path, live=True, model=None, max_total_tokens=1000)
    assert script.main(args) == 2


def test_report_skeleton_keeps_unknown_fields_null(tmp_path: Path) -> None:
    args = _args(tmp_path, model="openai/x", max_total_tokens=5000)
    assert script.main(args) == 0
    payload = json.loads((tmp_path / "report.json").read_text())
    assert payload["schema_version"] == 2
    assert payload["model_identifier"] == "openai/x"
    assert payload["max_total_tokens"] == 5000
    assert payload["status"] == "LIVE_NOT_RUN"
    assert payload["valid"] is None
    assert payload["invalid_reasons"] is None
    assert payload["tokens_spent_total"] is None
    assert payload["notes"]


def test_low_budget_is_refused_before_any_provider_call(tmp_path: Path) -> None:
    # A budget too small to reserve even one request must be refused up front;
    # no provider configuration is touched and no report arm is produced.
    args = _args(tmp_path, live=True, model="openai/x", max_total_tokens=10)
    assert script.main(args) == 2
    payload = json.loads((tmp_path / "report.json").read_text())
    assert payload["status"] == "LIVE_BLOCKED"
    assert payload["full_context"] is None
    assert payload["real_summary"] is None
