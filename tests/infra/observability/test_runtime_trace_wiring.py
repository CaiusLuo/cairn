from pathlib import Path

from cairn.agent.budget import RunBudget
from cairn.agent.models.offline import OfflineDevelopmentModel
from cairn.development.execution.runtime import ServiceExecution


def test_dynamic_secrets_reach_the_default_trace_redactor(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    execution = ServiceExecution(
        source,
        tmp_path / "state",
        tmp_path / "worktrees",
        OfflineDevelopmentModel,
        budget=RunBudget(1),
        sensitive_values=("initial-secret",),
        trace_root=tmp_path / "traces",
    )

    # Channel credentials are discovered after the Server has started.
    execution.protect_secrets(("late-secret",))

    assert execution.tracer is not None
    span = execution.tracer.start_root_span(
        "agent.turn",
        attributes={"value": "late-secret", "other": "initial-secret"},
    )
    execution.tracer.end_span(span)

    text = (tmp_path / "traces" / f"{span.context.trace_id}.jsonl").read_text()
    assert "late-secret" not in text
    assert "initial-secret" not in text
    assert text.count("[redacted]") == 2
