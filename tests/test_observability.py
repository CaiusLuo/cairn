import json
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

import cairn.trace_ui as trace_ui
from cairn.observability.models import Span, new_trace_context
from cairn.observability.reader import JsonlTraceReader
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


def test_reader_restores_spans(tmp_path: Path) -> None:
    tracer = Tracer(JsonlTraceSink(tmp_path))
    root = tracer.start_root_span("agent.turn")
    child = tracer.start_child_span(root, "llm.generate")

    tracer.end_span(child)
    tracer.end_span(root)

    spans = JsonlTraceReader(tmp_path).read(root.context.trace_id)

    assert len(spans) == 2
    assert {span.name for span in spans} == {"agent.turn", "llm.generate"}
    assert spans == [child, root]


@pytest.mark.parametrize("trace_id", ["", "../outside", "/tmp/outside"])
def test_reader_rejects_trace_ids_outside_root(tmp_path: Path, trace_id: str) -> None:
    with pytest.raises(ValueError, match="Invalid trace ID"):
        JsonlTraceReader(tmp_path).read(trace_id)


def test_print_trace_renders_each_span_once(monkeypatch: pytest.MonkeyPatch) -> None:
    output = StringIO()
    monkeypatch.setattr(
        trace_ui,
        "console",
        Console(file=output, color_system=None, force_terminal=False),
    )
    tracer = Tracer(RecordingSink())
    root = tracer.start_root_span("agent.turn")
    child = tracer.start_child_span(root, "llm.generate")

    trace_ui.print_trace([root, child])

    rendered = output.getvalue()
    assert rendered.count("agent.turn") == 1
    assert rendered.count("llm.generate") == 1
    assert rendered.count("[running]") == 2
