import json
from datetime import UTC, datetime, timedelta
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


def _write_trace_root(sink: JsonlTraceSink, start_time: datetime) -> Span:
    root = Span(
        context=new_trace_context(),
        name="agent.turn",
        start_time=start_time,
    )
    sink.emit(root)
    return root


def test_reader_lists_traces_no_repeated_resolver_scans(tmp_path: Path) -> None:
    """Verify list_traces uses _read_path, not resolver, avoiding O(N) directory scans.

    Writes 4 distinct trace files, then asserts list_traces returns those 4 roots
    without calling resolver.resolve (call count == 0). read() with a full ID still
    goes through resolver and works independently.
    """
    # Write exactly 4 trace files and remember their IDs.
    expected_ids: list[str] = []
    now = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(4):
        root = _write_trace_root(JsonlTraceSink(tmp_path), now + timedelta(minutes=i))
        expected_ids.append(root.context.trace_id)

    reader = JsonlTraceReader(tmp_path)

    # Count resolver.resolve calls; list_traces must not invoke it.
    calls: list[int] = [0]
    original_resolve = reader.resolver.resolve

    def counting_resolve(trace_id: str) -> str:
        calls[0] += 1
        return original_resolve(trace_id)

    reader.resolver.resolve = counting_resolve

    roots = reader.list_traces()
    assert len(roots) == 4
    assert calls[0] == 0, (
        f"list_traces called resolver.resolve {calls[0]} times; expected 0 (uses _read_path)"
    )
    # Verify the returned roots match what we wrote.
    actual_ids = {r.context.trace_id for r in roots}
    assert actual_ids == set(expected_ids), (
        f"Expected {set(expected_ids)}, got {actual_ids}"
    )

    # Read one full ID through resolver to confirm it still works.
    first_root = roots[0]
    read_spans = reader.read(first_root.context.trace_id)
    # read() may return just the root if no child was emitted; assert the root is present.
    assert any(span.name == "agent.turn" for span in read_spans)


def test_reader_lists_traces_newest_first(tmp_path: Path) -> None:
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


def test_reader_limits_trace_list(tmp_path: Path) -> None:
    sink = JsonlTraceSink(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    _write_trace_root(sink, now)
    middle = _write_trace_root(sink, now + timedelta(minutes=1))
    newest = _write_trace_root(sink, now + timedelta(minutes=2))

    roots = JsonlTraceReader(tmp_path).list_traces(limit=2)

    assert [root.context.trace_id for root in roots] == [
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
