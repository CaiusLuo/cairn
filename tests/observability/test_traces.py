import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cairn.core.agent import Agent
from cairn.core.budget import RunBudget
from cairn.core.events import Event
from cairn.core.loop import run_turn
from cairn.core.models import LLMResponse
from cairn.observability.models import (
    Span,
    SpanStatus,
    TraceSummary,
    child_context,
    new_trace_context,
)
from cairn.observability.reader import JsonlTraceReader
from cairn.observability.sinks import JsonlTraceSink
from cairn.observability.tracer import Tracer
from cairn.tools.registry import ToolRegistry
from tests.support.runtime import SequenceLLM


def _root(
    start_time: datetime, *, trace_id: str | None = None, completed: bool = True
) -> Span:
    context = new_trace_context()
    if trace_id is not None:
        context.trace_id = trace_id
    return Span(
        context=context,
        name="agent.turn",
        start_time=start_time,
        end_time=start_time + timedelta(seconds=1) if completed else None,
        status=SpanStatus.OK if completed else SpanStatus.UNSET,
    )


def _write_detail(root: Path, spans: list[Span]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{spans[0].context.trace_id}.jsonl"
    path.write_text("".join(f"{span.model_dump_json()}\n" for span in spans))


def test_tracer_persists_and_restores_parent_child_spans(tmp_path: Path) -> None:
    sink = JsonlTraceSink(tmp_path)
    sink.emit(_root(datetime.now(UTC), completed=False))
    assert not (tmp_path / "summaries").exists()
    tracer = Tracer(sink)
    root = tracer.start_root_span("agent.turn")
    child = tracer.start_child_span(root, "llm.generate")

    tracer.end_span(child)
    assert not (tmp_path / "summaries").exists()
    tracer.end_span(root)

    spans = JsonlTraceReader(tmp_path).read(root.context.trace_id)

    assert spans == [child, root]
    assert child.context.trace_id == root.context.trace_id
    assert child.context.parent_span_id == root.context.span_id
    summary_path = tmp_path / "summaries" / f"{root.context.trace_id}.json"
    saved_summary = summary_path.read_bytes()
    later_child = tracer.start_child_span(root, "llm.generate")
    tracer.end_span(later_child)
    assert summary_path.read_bytes() == saved_summary
    assert (tmp_path / "summaries" / f"{root.context.trace_id}.json").is_file()


def test_reader_lists_newest_first_and_fast_path_uses_no_detail_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = JsonlTraceSink(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    roots = [
        _root(start + timedelta(minutes=index), trace_id=f"{19 - index:032x}")
        for index in range(20)
    ]
    for item in roots:
        for child_index in range(20):
            child_start = item.start_time + timedelta(milliseconds=child_index)
            sink.emit(
                Span(
                    context=child_context(item.context),
                    name="llm.generate",
                    start_time=child_start,
                    end_time=child_start + timedelta(milliseconds=1),
                    status=SpanStatus.OK,
                )
            )
        sink.emit(item)

    reader = JsonlTraceReader(tmp_path)

    def unexpected_read(_stem: str) -> list[Span]:
        raise AssertionError("summary listing read JSONL details")

    def unexpected_resolve(_trace_id: str) -> str:
        raise AssertionError("summary listing used the strict resolver")

    monkeypatch.setattr(reader, "_read_path", unexpected_read)
    monkeypatch.setattr(reader.resolver, "resolve", unexpected_resolve)
    result = reader.list_traces(limit=1)

    assert len(result.traces) == 1
    assert result.traces[0].trace_id == roots[-1].context.trace_id
    assert result.diagnostics == []
    assert (tmp_path / "summaries" / f"{roots[-1].context.trace_id}.json").is_file()


def test_legacy_listing_reads_each_detail_once_then_uses_backfilled_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = [
        _root(datetime(2026, 2, 1, tzinfo=UTC) + timedelta(minutes=n)) for n in range(3)
    ]
    for root in roots:
        _write_detail(tmp_path, [root])
    reader = JsonlTraceReader(tmp_path)
    reads: list[str] = []
    resolves: list[str] = []
    original_read = reader._read_path
    original_resolve = reader.resolver.resolve

    def count_read(stem: str) -> list[Span]:
        reads.append(stem)
        return original_read(stem)

    def count_resolve(trace_id: str) -> str:
        resolves.append(trace_id)
        return original_resolve(trace_id)

    monkeypatch.setattr(reader, "_read_path", count_read)
    monkeypatch.setattr(reader.resolver, "resolve", count_resolve)
    first = reader.list_traces()
    assert len(reads) == len(roots)
    assert resolves == []
    assert len(first.traces) == len(roots)
    assert len(list((tmp_path / "summaries").glob("*.json"))) == len(roots)

    reads.clear()
    second = reader.list_traces()
    assert reads == []
    assert [item.trace_id for item in second.traces] == [
        item.trace_id for item in first.traces
    ]


@pytest.mark.parametrize(
    ("kind", "diagnostic"),
    [
        ("malformed", "skipped corrupt trace"),
        ("truncated", "skipped corrupt trace"),
        ("incomplete", "skipped incomplete trace"),
        ("bad_summary", "recovered summary for trace"),
        ("identity", "recovered summary for trace"),
        ("timezone", "recovered summary for trace"),
        ("bad_both", "skipped corrupt trace"),
    ],
)
def test_listing_recovers_or_skips_bad_entries_without_losing_valid_traces(
    tmp_path: Path, kind: str, diagnostic: str
) -> None:
    start = datetime(2026, 3, 1, tzinfo=UTC)
    good_before = _root(start)
    good_after = _root(start + timedelta(minutes=3))
    bad = _root(start + timedelta(minutes=2), completed=kind != "incomplete")
    _write_detail(tmp_path, [good_before])
    _write_detail(tmp_path, [good_after])
    _write_detail(tmp_path, [bad])
    summary_dir = tmp_path / "summaries"
    summary_dir.mkdir()
    if kind in {"malformed", "truncated", "bad_both"}:
        detail = tmp_path / f"{bad.context.trace_id}.jsonl"
        detail.write_text(
            "not-json\n" if kind != "truncated" else bad.model_dump_json() + "\n{"
        )
    if kind == "bad_both":
        (summary_dir / f"{bad.context.trace_id}.json").write_text("{")
    elif kind in {"bad_summary", "identity", "timezone"}:
        summary = TraceSummary.from_root(bad).model_dump(mode="json")
        if kind == "identity":
            summary["trace_id"] = "wrong-id"
        elif kind == "timezone":
            summary["end_time"] = (
                datetime.fromisoformat(summary["end_time"])
                .replace(tzinfo=None)
                .isoformat()
            )
        else:
            (summary_dir / f"{bad.context.trace_id}.json").write_text("{")
            summary = {}
        if summary:
            summary_dir.joinpath(f"{bad.context.trace_id}.json").write_text(
                json.dumps(summary)
            )

    if kind in {"malformed", "truncated"}:
        assert not (summary_dir / f"{bad.context.trace_id}.json").exists()
    result = JsonlTraceReader(tmp_path).list_traces()

    valid_ids = {
        good_before.context.trace_id,
        good_after.context.trace_id,
    }
    if kind in {"bad_summary", "identity", "timezone"}:
        valid_ids.add(bad.context.trace_id)
    assert {item.trace_id for item in result.traces} == valid_ids
    assert any(diagnostic in item for item in result.diagnostics)
    if kind in {"bad_summary", "identity", "timezone"}:
        recovered = TraceSummary.model_validate_json(
            (summary_dir / f"{bad.context.trace_id}.json").read_text()
        )
        assert recovered == TraceSummary.from_root(bad)


def test_valid_summary_survives_corrupt_detail_but_strict_read_stays_strict(
    tmp_path: Path,
) -> None:
    root = _root(datetime(2026, 4, 1, tzinfo=UTC))
    sink = JsonlTraceSink(tmp_path)
    sink.emit(root)
    with (tmp_path / f"{root.context.trace_id}.jsonl").open("a") as trace_file:
        trace_file.write("{\n")

    assert (
        JsonlTraceReader(tmp_path).list_traces().traces[0].trace_id
        == root.context.trace_id
    )
    with pytest.raises(ValueError, match="Invalid JSON"):
        JsonlTraceReader(tmp_path).read(root.context.trace_id)


def test_strict_read_keeps_prefix_resolution_and_ignores_summary_sidecars(
    tmp_path: Path,
) -> None:
    root = _root(datetime.now(UTC), trace_id="abcd1111")
    _write_detail(tmp_path, [root])
    summary_dir = tmp_path / "summaries"
    summary_dir.mkdir()
    (summary_dir / "abcd1111.json").write_text(
        TraceSummary.from_root(root).model_dump_json()
    )
    (summary_dir / "abcd2222.json").write_text("sidecar only")

    reader = JsonlTraceReader(tmp_path)
    assert reader.read("abcd1111") == [root]
    assert reader.read("abcd") == [root]
    _write_detail(tmp_path, [_root(datetime.now(UTC), trace_id="abcd2222")])
    with pytest.raises(ValueError, match="Ambiguous trace prefix"):
        reader.read("abcd")


@pytest.mark.parametrize("trace_id", ["", "../outside", "/tmp/outside"])
def test_reader_rejects_trace_ids_outside_root(tmp_path: Path, trace_id: str) -> None:
    with pytest.raises(ValueError, match="Invalid trace ID"):
        JsonlTraceReader(tmp_path).read(trace_id)


def test_backfill_write_failure_is_nonfatal_and_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cairn.observability import reader

    root = _root(datetime.now(UTC))
    _write_detail(tmp_path, [root])

    def fail_write(_root: Path, _summary: TraceSummary) -> None:
        raise OSError("cache unavailable")

    monkeypatch.setattr(reader, "write_summary", fail_write)
    result = JsonlTraceReader(tmp_path).list_traces()

    assert [item.trace_id for item in result.traces] == [root.context.trace_id]
    assert result.diagnostics == [
        f"could not save summary for trace {root.context.trace_id[:8]} (OSError)"
    ]


def test_atomic_summary_replace_preserves_old_file_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cairn.observability import sinks

    root = _root(datetime.now(UTC))
    previous = TraceSummary.from_root(root)
    sinks.write_summary(tmp_path, previous)
    target = tmp_path / "summaries" / f"{root.context.trace_id}.json"
    old_contents = target.read_text()
    replacement = TraceSummary.from_root(
        root.model_copy(update={"end_time": root.start_time + timedelta(seconds=2)})
    )
    replaced: list[bool] = []
    real_replace = os.replace

    def inspect_replace(source: str | Path, destination: str | Path) -> None:
        assert target.read_text() == old_contents
        assert TraceSummary.model_validate_json(Path(source).read_text()) == replacement
        replaced.append(True)
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", inspect_replace)
    sinks.write_summary(tmp_path, replacement)
    assert replaced == [True]
    replacement_contents = target.read_text()
    assert replacement_contents != old_contents
    assert TraceSummary.model_validate_json(replacement_contents) == replacement
    assert list(target.parent.glob("*.tmp")) == []

    def fail_replace(_source: str | Path, _destination: str | Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    later = TraceSummary.from_root(
        root.model_copy(update={"end_time": root.start_time + timedelta(seconds=3)})
    )
    with pytest.raises(OSError, match="replace failed"):
        sinks.write_summary(tmp_path, later)
    assert target.read_text() == replacement_contents
    assert list(target.parent.glob("*.tmp")) == []


def test_summary_failure_keeps_run_result_and_strict_jsonl_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cairn.observability import sinks

    events: list[Event] = []
    sink = JsonlTraceSink(tmp_path)
    agent = Agent(
        llm=SequenceLLM([LLMResponse(content="done")]),
        tools=ToolRegistry(),
        tracer=Tracer(sink),
        event_handler=lambda event: events.append(event),
    )

    def fail_summary(_root: Path, _summary: TraceSummary) -> None:
        raise OSError("summary write failed")

    monkeypatch.setattr(sinks, "write_summary", fail_summary)
    assert (
        asyncio.run(run_turn(agent, "hello", budget=RunBudget(max_steps=1))) == "done"
    )
    finished = next(event for event in events if event.type == "trace_finish")
    assert finished.data["persisted"] is False
    assert "summary write failed" in finished.data["persistence_error"]
    trace_id = events[0].data["trace_id"]
    spans = JsonlTraceReader(tmp_path).read(str(trace_id))
    assert spans[-1].context.trace_id == trace_id
