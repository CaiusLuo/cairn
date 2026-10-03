import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import cairn.observability.storage as storage
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
from cairn.observability.sinks import JsonlTraceSink
from cairn.observability.storage import TAIL_WINDOW_BYTES, TraceStore
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


def _child(parent: Span) -> Span:
    return Span(
        context=child_context(parent.context),
        name="llm.generate",
        start_time=parent.start_time,
        end_time=parent.start_time + timedelta(milliseconds=1),
        status=SpanStatus.OK,
    )


def _write_detail(root: Path, spans: list[Span]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{spans[0].context.trace_id}.jsonl"
    path.write_text("".join(f"{span.model_dump_json()}\n" for span in spans))


def _unexpected_detail_read(self: TraceStore, stem: str) -> list[Span]:
    raise AssertionError(f"the whole detail of {stem} was read")


def test_tracer_persists_and_restores_parent_child_spans(tmp_path: Path) -> None:
    sink = JsonlTraceSink(tmp_path)
    sink.emit(_root(datetime.now(UTC), completed=False))
    tracer = Tracer(sink)
    root = tracer.start_root_span("agent.turn")
    child = tracer.start_child_span(root, "llm.generate")

    tracer.end_span(child)
    tracer.end_span(root)

    spans = TraceStore(tmp_path).read(root.context.trace_id)

    assert spans == [child, root]
    assert child.context.trace_id == root.context.trace_id
    assert child.context.parent_span_id == root.context.span_id
    # One file per trace: the JSONL is the only thing ever written.
    assert not (tmp_path / "summaries").exists()


def test_later_child_spans_only_append_to_the_jsonl(tmp_path: Path) -> None:
    sink = JsonlTraceSink(tmp_path)
    tracer = Tracer(sink)
    root = tracer.start_root_span("agent.turn")
    tracer.end_span(root)
    detail = tmp_path / f"{root.context.trace_id}.jsonl"
    after_root = detail.read_bytes()

    later_child = tracer.start_child_span(root, "llm.generate")
    tracer.end_span(later_child)

    assert detail.read_bytes() != after_root
    assert not (tmp_path / "summaries").exists()


def test_run_turn_persists_a_single_jsonl_file(tmp_path: Path) -> None:
    events: list[Event] = []
    agent = Agent(
        llm=SequenceLLM([LLMResponse(content="done")]),
        tools=ToolRegistry(),
        tracer=Tracer(JsonlTraceSink(tmp_path)),
        event_handler=lambda event: events.append(event),
    )

    assert (
        asyncio.run(run_turn(agent, "hello", budget=RunBudget(max_steps=1))) == "done"
    )

    finished = next(event for event in events if event.type == "trace_finish")
    trace_id = str(events[0].data["trace_id"])
    assert finished.data["persisted"] is True
    assert TraceStore(tmp_path).read(trace_id)[-1].context.trace_id == trace_id
    assert not (tmp_path / "summaries").exists()


def test_count_is_storage_level_and_never_parses_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_detail(tmp_path, [_root(datetime.now(UTC))])
    (tmp_path / "broken.jsonl").write_text("garbage")
    monkeypatch.setattr(TraceStore, "_read_detail", _unexpected_detail_read)

    def unexpected_root_record(_path: Path) -> tuple[dict[str, Any] | None, bool]:
        raise AssertionError("count parsed a trace file")

    monkeypatch.setattr(
        TraceStore, "_root_record", staticmethod(unexpected_root_record)
    )

    assert TraceStore(tmp_path).count() == 2


def test_count_of_a_missing_directory_is_zero(tmp_path: Path) -> None:
    assert TraceStore(tmp_path / "absent").count() == 0


def test_listing_is_newest_first_and_reads_only_a_bounded_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    roots = [
        _root(start + timedelta(minutes=index), trace_id=f"{index:032x}")
        for index in range(3)
    ]
    for item in roots:
        _write_detail(tmp_path, [*[_child(item) for _ in range(2000)], item])
    assert all(
        (tmp_path / f"{item.context.trace_id}.jsonl").stat().st_size > TAIL_WINDOW_BYTES
        for item in roots
    )

    windows: list[int] = []
    real_tail_bytes = storage._tail_bytes

    def spy(path: Path, size: int, window: int) -> bytes:
        windows.append(window)
        return real_tail_bytes(path, size, window)

    monkeypatch.setattr(storage, "_tail_bytes", spy)
    monkeypatch.setattr(TraceStore, "_read_detail", _unexpected_detail_read)

    result = TraceStore(tmp_path).list_traces()

    assert [item.trace_id for item in result.traces] == [
        item.context.trace_id for item in reversed(roots)
    ]
    assert result.diagnostics == []
    assert windows and max(windows) <= TAIL_WINDOW_BYTES


def test_root_beyond_the_tail_window_is_still_found(tmp_path: Path) -> None:
    root = _root(datetime(2026, 1, 1, tzinfo=UTC))
    _write_detail(tmp_path, [root, *[_child(root) for _ in range(2000)]])

    result = TraceStore(tmp_path).list_traces()

    assert [item.trace_id for item in result.traces] == [root.context.trace_id]
    assert result.diagnostics == []


def test_torn_trailing_append_does_not_hide_a_completed_trace(tmp_path: Path) -> None:
    root = _root(datetime.now(UTC))
    _write_detail(tmp_path, [root])
    detail = tmp_path / f"{root.context.trace_id}.jsonl"
    with detail.open("a", encoding="utf-8") as trace_file:
        trace_file.write('{"context": {"trace_id": "torn"')

    result = TraceStore(tmp_path).list_traces()

    assert [item.trace_id for item in result.traces] == [root.context.trace_id]
    assert result.diagnostics == []


def test_read_skips_blank_lines_and_reports_a_vanished_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(datetime.now(UTC))
    _write_detail(tmp_path, [root])
    detail = tmp_path / f"{root.context.trace_id}.jsonl"
    with detail.open("a", encoding="utf-8") as trace_file:
        trace_file.write("\n")

    store = TraceStore(tmp_path)
    assert store.read(root.context.trace_id) == [root]

    real_resolve = store.resolver.resolve

    def resolve_then_delete(trace_id: str) -> str:
        resolved = real_resolve(trace_id)
        (tmp_path / f"{resolved}.jsonl").unlink()
        return resolved

    monkeypatch.setattr(store.resolver, "resolve", resolve_then_delete)

    with pytest.raises(FileNotFoundError, match="Trace not found"):
        store.read(root.context.trace_id)


def test_listing_reports_an_identity_mismatch_as_corrupt(tmp_path: Path) -> None:
    mismatched = _root(datetime.now(UTC), trace_id="cafebabe")
    (tmp_path / "deadbeef.jsonl").write_text(f"{mismatched.model_dump_json()}\n")

    result = TraceStore(tmp_path).list_traces()

    assert result.traces == []
    assert result.diagnostics == ["skipped corrupt trace deadbeef (ValueError)"]


def test_summary_requires_a_completed_root_span_and_consistent_timezones() -> None:
    with pytest.raises(ValueError, match="completed root span"):
        TraceSummary.from_root(_root(datetime.now(UTC), completed=False))

    with pytest.raises(ValueError, match="consistent timezones"):
        TraceSummary(
            trace_id="x",
            name="agent.turn",
            start_time=datetime(2026, 1, 1, tzinfo=UTC),
            end_time=datetime(2026, 1, 1),
            status=SpanStatus.OK,
        )


def test_legacy_summary_sidecars_are_discarded_on_listing(tmp_path: Path) -> None:
    start = datetime(2026, 2, 1, tzinfo=UTC)
    roots = [_root(start + timedelta(minutes=index)) for index in range(3)]
    for item in roots:
        _write_detail(tmp_path, [item])
    summary_dir = tmp_path / "summaries"
    summary_dir.mkdir()
    for item in roots:
        (summary_dir / f"{item.context.trace_id}.json").write_text(
            TraceSummary.from_root(item).model_dump_json()
        )
    (summary_dir / "orphan-left-behind.json").write_text("{}")

    result = TraceStore(tmp_path).list_traces()

    assert [item.trace_id for item in result.traces] == [
        item.context.trace_id for item in reversed(roots)
    ]
    assert result.diagnostics == []
    # Sidecars are derived data now: they are removed, and so is the empty dir.
    assert not summary_dir.exists()


def test_legacy_sidecar_of_an_unreadable_trace_is_kept(tmp_path: Path) -> None:
    incomplete = _root(datetime.now(UTC), completed=False)
    _write_detail(tmp_path, [incomplete])
    summary_dir = tmp_path / "summaries"
    summary_dir.mkdir()
    sidecar = summary_dir / f"{incomplete.context.trace_id}.json"
    sidecar.write_text("{}")

    result = TraceStore(tmp_path).list_traces()

    assert result.traces == []
    assert any("skipped incomplete trace" in item for item in result.diagnostics)
    # Only sidecars known to be redundant are dropped.
    assert sidecar.exists()


@pytest.mark.parametrize(
    ("kind", "diagnostic"),
    [
        ("malformed", "skipped corrupt trace"),
        ("non-object", "skipped corrupt trace"),
        ("truncated-root", "skipped corrupt trace"),
        ("empty", "skipped incomplete trace"),
        ("incomplete", "skipped incomplete trace"),
    ],
)
def test_listing_skips_unreadable_traces_without_losing_valid_ones(
    tmp_path: Path, kind: str, diagnostic: str
) -> None:
    start = datetime(2026, 3, 1, tzinfo=UTC)
    good_before = _root(start)
    good_after = _root(start + timedelta(minutes=3))
    bad = _root(start + timedelta(minutes=2), completed=kind != "incomplete")
    _write_detail(tmp_path, [good_before])
    _write_detail(tmp_path, [good_after])
    bad_path = tmp_path / f"{bad.context.trace_id}.jsonl"

    if kind == "incomplete":
        _write_detail(tmp_path, [bad])
    elif kind == "malformed":
        bad_path.write_text("not-json\n")
    elif kind == "non-object":
        bad_path.write_text("123\n")
    elif kind == "truncated-root":
        bad_path.write_text(bad.model_dump_json()[:-10])
    else:
        bad_path.write_text("")

    result = TraceStore(tmp_path).list_traces()

    assert {item.trace_id for item in result.traces} == {
        good_before.context.trace_id,
        good_after.context.trace_id,
    }
    assert any(diagnostic in item for item in result.diagnostics)


def test_strict_read_resolves_prefixes_and_names_ambiguous_candidates(
    tmp_path: Path,
) -> None:
    first = _root(datetime.now(UTC), trace_id="abcd1111")
    _write_detail(tmp_path, [first])
    store = TraceStore(tmp_path)

    assert store.read("abcd1111") == [first]
    assert store.read("abcd") == [first]

    second = _root(datetime.now(UTC), trace_id="abcd2222")
    _write_detail(tmp_path, [second])

    with pytest.raises(
        ValueError,
        match=r"Ambiguous trace prefix: abcd \(2 matches: abcd1111, abcd2222\)",
    ):
        store.read("abcd")


def test_read_ignores_legacy_summary_sidecars(tmp_path: Path) -> None:
    root = _root(datetime.now(UTC), trace_id="abcd1111")
    _write_detail(tmp_path, [root])
    summary_dir = tmp_path / "summaries"
    summary_dir.mkdir()
    (summary_dir / "abcd1111.json").write_text("sidecar only")

    assert TraceStore(tmp_path).read("abcd") == [root]
    assert (summary_dir / "abcd1111.json").exists()


@pytest.mark.parametrize("trace_id", ["", "../outside", "/tmp/outside"])
def test_reader_rejects_trace_ids_outside_root(tmp_path: Path, trace_id: str) -> None:
    with pytest.raises(ValueError, match="Invalid trace ID"):
        TraceStore(tmp_path).read(trace_id)


def test_delete_removes_the_derived_cache_before_the_source_of_truth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(datetime.now(UTC))
    _write_detail(tmp_path, [root])
    summary_dir = tmp_path / "summaries"
    summary_dir.mkdir()
    sidecar = summary_dir / f"{root.context.trace_id}.json"
    sidecar.write_text(TraceSummary.from_root(root).model_dump_json())

    removed: list[str] = []
    real_unlink = Path.unlink

    def spy(self: Path, *args: Any, **kwargs: Any) -> None:
        removed.append(self.name)
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", spy)

    result = TraceStore(tmp_path).delete(root.context.trace_id[:8])

    assert result.deleted == [root.context.trace_id]
    assert result.skipped == []
    assert removed == [
        f"{root.context.trace_id}.json",
        f"{root.context.trace_id}.jsonl",
    ]
    assert not sidecar.exists()
    assert not (tmp_path / f"{root.context.trace_id}.jsonl").exists()


def test_delete_can_remove_incomplete_and_corrupt_traces(tmp_path: Path) -> None:
    store = TraceStore(tmp_path)
    (tmp_path / "deadbeef.jsonl").write_text("not-json\n")

    assert store.delete("deadbeef").deleted == ["deadbeef"]
    assert store.count() == 0


def test_delete_reports_unknown_and_ambiguous_ids_without_removing_anything(
    tmp_path: Path,
) -> None:
    for trace_id in ("abcd1111", "abcd2222"):
        _write_detail(tmp_path, [_root(datetime.now(UTC), trace_id=trace_id)])
    store = TraceStore(tmp_path)

    with pytest.raises(FileNotFoundError, match="Trace not found"):
        store.delete("ffff")
    with pytest.raises(ValueError, match="Ambiguous trace prefix"):
        store.delete("abcd")

    assert store.count() == 2


def test_delete_oldest_removes_the_tail_of_the_listing_and_skips_unreadable(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 5, 1, tzinfo=UTC)
    traces = [_root(start + timedelta(minutes=index)) for index in range(4)]
    for item in traces:
        _write_detail(tmp_path, [item])
    (tmp_path / "badbadba.jsonl").write_text("not-json\n")

    result = TraceStore(tmp_path).delete_oldest(2)

    assert result.deleted == [
        traces[0].context.trace_id,
        traces[1].context.trace_id,
    ]
    assert result.skipped == ["skipped corrupt trace badbadba"]
    assert {path.stem for path in tmp_path.glob("*.jsonl")} == {
        traces[2].context.trace_id,
        traces[3].context.trace_id,
        "badbadba",
    }


@pytest.mark.parametrize("count", [0, -1])
def test_delete_oldest_rejects_non_positive_counts(tmp_path: Path, count: int) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        TraceStore(tmp_path).delete_oldest(count)


def test_delete_oldest_keeps_going_when_one_delete_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = datetime(2026, 6, 1, tzinfo=UTC)
    traces = [_root(start + timedelta(minutes=index)) for index in range(3)]
    for item in traces:
        _write_detail(tmp_path, [item])
    locked = f"{traces[0].context.trace_id}.jsonl"
    real_unlink = Path.unlink

    def flaky(self: Path, *args: Any, **kwargs: Any) -> None:
        if self.name == locked:
            raise PermissionError("locked")
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky)

    result = TraceStore(tmp_path).delete_oldest(2)

    assert result.deleted == [traces[1].context.trace_id]
    assert result.skipped == [
        f"could not delete trace {traces[0].context.trace_id[:8]} (PermissionError)"
    ]
    assert (tmp_path / locked).exists()
