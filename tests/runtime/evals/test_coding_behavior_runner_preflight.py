"""The real behavior-eval runner refuses to run when the sandbox is unavailable.

Behavior grading executes model-generated code; if no isolation is available the
runner must block up front rather than grade generated code on the host.
"""

import asyncio
from pathlib import Path

from evaluation.runners import run_coding_behavior as script


def test_runner_blocks_when_isolation_unavailable(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(script, "sandbox_available", lambda: False)
    destination = tmp_path / "report.json"
    assert asyncio.run(script.main(destination)) == 2
    assert not destination.exists()
