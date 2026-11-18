import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cairn.agent.tracing.models import Span, SpanStatus, new_trace_context, start_span
from cairn.agent.tracing.retention import RetentionTraceSink
from cairn.agent.tracing.sinks import JsonlTraceSink


def _root_span() -> Span:
    span = start_span("agent.turn", new_trace_context())
    span.end_time = datetime.now(UTC)
    span.status = SpanStatus.OK
    return span


def test_sweep_enforces_age_then_file_count(tmp_path: Path) -> None:
    now = time.time()
    for index, age_days in enumerate((40, 30, 3, 2, 1)):
        path = tmp_path / f"{index:032x}.jsonl"
        path.write_text("{}\n")
        os.utime(path, (now - age_days * 86400, now - age_days * 86400))

    sink = RetentionTraceSink(
        JsonlTraceSink(tmp_path), tmp_path, max_files=2, max_age_days=7
    )

    assert sink.sweep() == 3
    assert sorted(path.name for path in tmp_path.glob("*.jsonl")) == [
        f"{3:032x}.jsonl",
        f"{4:032x}.jsonl",
    ]


def test_completed_root_span_writes_first_then_prunes(tmp_path: Path) -> None:
    sink = RetentionTraceSink(
        JsonlTraceSink(tmp_path), tmp_path, max_files=1, sweep_every=1
    )
    first = _root_span()
    sink.emit(first)
    os.utime(
        tmp_path / f"{first.context.trace_id}.jsonl",
        (time.time() - 10, time.time() - 10),
    )
    second = _root_span()
    sink.emit(second)

    assert [path.stem for path in tmp_path.glob("*.jsonl")] == [second.context.trace_id]


def test_write_failure_still_propagates_to_the_tracer(tmp_path: Path) -> None:
    class BrokenSink:
        def emit(self, span: object) -> None:
            raise OSError("disk unavailable")

    sink = RetentionTraceSink(BrokenSink(), tmp_path)

    with pytest.raises(OSError, match="disk unavailable"):
        sink.emit(_root_span())


def test_file_capacity_failure_is_reported_without_growing_trace(
    tmp_path: Path,
) -> None:
    from cairn.agent.tracing.tracer import Tracer

    tracer = Tracer(JsonlTraceSink(tmp_path, max_file_bytes=512))
    span = tracer.start_root_span("agent.turn", {"detail": "x" * 1024})
    tracer.end_span(span)
    assert tracer.pop_persistence_error(span.context.trace_id)
    assert (tmp_path / f"{span.context.trace_id}.jsonl").stat().st_size == 0
