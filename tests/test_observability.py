import json
from datetime import UTC, datetime
from pathlib import Path

from cairn.observability.models import Span, new_trace_context
from cairn.observability.sinks import JsonlTraceSink
from cairn.observability.tracer import Tracer


class RecordingSink:
    def __init__(self) -> None:
        self.spans: list[Span] = []

    def emit(self, span: Span) -> None:
        self.spans.append(span)


def test_tracer_creates_parent_child_relationship() -> None:
    sink = RecordingSink()
    tracer = Tracer(sink)

    root = tracer.start_root_span("agent.turn")
    child = tracer.start_child_span(root, "llm.generate")

    tracer.end_span(child)
    tracer.end_span(root)

    assert child.context.trace_id == root.context.trace_id
    assert child.context.parent_span_id == root.context.span_id

    assert len(sink.spans) == 2
    assert sink.spans[0].name == "llm.generate"
    assert sink.spans[1].name == "agent.turn"


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
