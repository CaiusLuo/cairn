import json
from datetime import UTC, datetime
from pathlib import Path

from cairn.observability.models import (
    Span,
    new_trace_context,
)
from cairn.observability.sinks import JsonlTraceSink


def test_jsonl_trace_sink_writes_span(tmp_path: Path) -> None:
    context = new_trace_context()

    span = Span(
        context=context,
        name="agent.turn",
        start_time=datetime(2026, 1, 1, tzinfo=UTC),
    )

    sink = JsonlTraceSink(tmp_path)

    sink.emit(span)

    path = tmp_path / f"{context.trace_id}.jsonl"

    lines = path.read_text(encoding="utf-8").splitlines()

    assert len(lines) == 1

    data = json.loads(lines[0])

    assert data["name"] == "agent.turn"
    assert data["context"]["trace_id"] == context.trace_id
    assert data["start_time"] == "2026-01-01T00:00:00Z"
