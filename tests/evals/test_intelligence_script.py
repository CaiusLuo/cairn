import asyncio
import json
from pathlib import Path

import pytest
from examples.evals import run_repository_intelligence as script


def test_unavailable_configuration_records_blocked_without_model_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable() -> None:
        raise ValueError("sensitive configuration details")

    monkeypatch.setattr(script, "load_project_providers", unavailable)
    assert asyncio.run(script.main(tmp_path / "reports")) == 2
    report = json.loads((tmp_path / "reports" / "experiment.json").read_text())
    assert report["state"] == "real_eval_blocked" and report["runs"] == []
    assert "sensitive" not in (tmp_path / "reports" / "findings.md").read_text()


def test_pilot_never_overwrites_previous_results(tmp_path: Path) -> None:
    existing = tmp_path / "findings.md"
    existing.write_text("previous pilot")
    with pytest.raises(ValueError, match="empty"):
        asyncio.run(script.main(tmp_path))
    assert existing.read_text() == "previous pilot"
