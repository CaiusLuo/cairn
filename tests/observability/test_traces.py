from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cairn.observability.models import Span, new_trace_context
from cairn.observability.reader import JsonlTraceReader
from cairn.observability.sinks import JsonlTraceSink
from cairn.observability.tracer import Tracer


def test_tracer_persists_and_restores_parent_child_spans(tmp_path: Path) -> None:
    tracer = Tracer(JsonlTraceSink(tmp_path))
    root = tracer.start_root_span("agent.turn")
    child = tracer.start_child_span(root, "llm.generate")

    tracer.end_span(child)
    tracer.end_span(root)

    spans = JsonlTraceReader(tmp_path).read(root.context.trace_id)

    assert spans == [child, root]
    assert child.context.trace_id == root.context.trace_id
    assert child.context.parent_span_id == root.context.span_id


def _write_trace_root(sink: JsonlTraceSink, start_time: datetime) -> Span:
    root = Span(
        context=new_trace_context(),
        name="agent.turn",
        start_time=start_time,
    )
    sink.emit(root)
    return root


def test_reader_lists_newest_first_and_applies_limit(tmp_path: Path) -> None:
    sink = JsonlTraceSink(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    newest = _write_trace_root(sink, now + timedelta(minutes=2))
    oldest = _write_trace_root(sink, now)
    middle = _write_trace_root(sink, now + timedelta(minutes=1))

    roots = JsonlTraceReader(tmp_path).list_traces()

    assert [root.context.trace_id for root in roots] == [
        newest.context.trace_id,
        middle.context.trace_id,
        oldest.context.trace_id,
    ]
    limited = JsonlTraceReader(tmp_path).list_traces(limit=2)

    assert [root.context.trace_id for root in limited] == [
        newest.context.trace_id,
        middle.context.trace_id,
    ]


def test_reader_resolves_unique_trace_prefix(tmp_path: Path) -> None:
    tracer = Tracer(JsonlTraceSink(tmp_path))
    root = tracer.start_root_span("agent.turn")
    tracer.end_span(root)

    spans = JsonlTraceReader(tmp_path).read(root.context.trace_id[:8])

    assert spans == [root]


def test_reader_rejects_ambiguous_trace_prefix(tmp_path: Path) -> None:
    for trace_id in ("abcd1111", "abcd2222"):
        (tmp_path / f"{trace_id}.jsonl").write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="Ambiguous trace prefix"):
        JsonlTraceReader(tmp_path).read("abcd")


@pytest.mark.parametrize("trace_id", ["", "../outside", "/tmp/outside"])
def test_reader_rejects_trace_ids_outside_root(tmp_path: Path, trace_id: str) -> None:
    with pytest.raises(ValueError, match="Invalid trace ID"):
        JsonlTraceReader(tmp_path).read(trace_id)
