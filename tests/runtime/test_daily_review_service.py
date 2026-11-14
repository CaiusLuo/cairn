"""Daily review generation is bounded, cited, idempotent and restart-safe."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from cairn.agent.models.messages import LLMResponse, Message, ToolCall
from cairn.knowledge.store import KnowledgeStore
from cairn.review.collector import ReviewSourceCollector
from cairn.review.models import (
    ReviewInput,
    ReviewStatus,
    ReviewType,
    SourceCollectionState,
    SourceCollectionStatus,
    SourceSnapshot,
    SourceTrust,
)
from cairn.review.service import NO_SOURCE_MESSAGES, DailyReviewService
from cairn.review.store import ReviewPeriodCompleted, ReviewStore

DAY = date(2026, 1, 15)
TZ = "Asia/Shanghai"
MODEL_ID = "test-model"
START = datetime(2026, 1, 15, tzinfo=ZoneInfo(TZ))
END = datetime(2026, 1, 16, tzinfo=ZoneInfo(TZ))


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source(
    source_id: str = "run-1",
    *,
    may_prove_completion: bool = True,
    snapshot: str = '{"run_id":"run-1","status":"completed"}',
) -> SourceSnapshot:
    return SourceSnapshot(
        source_type="development_run",
        source_id=source_id,
        content_hash=_hash(snapshot),
        occurred_at="2026-01-15T10:00:00+08:00",
        trust=SourceTrust.SYSTEM_RECEIPT,
        may_prove_completion=may_prove_completion,
        verification="run_status=completed",
        snapshot=snapshot,
    )


def _input(
    sources: tuple[SourceSnapshot, ...] | None = None,
    *,
    day: date = DAY,
) -> ReviewInput:
    selected = (_source(),) if sources is None else sources
    status = SourceCollectionStatus(
        source_type="development_run",
        status=SourceCollectionState.OK if selected else SourceCollectionState.EMPTY,
        discovered=len(selected),
        included=len(selected),
    )
    start = datetime(day.year, day.month, day.day, tzinfo=ZoneInfo(TZ))
    return ReviewInput.build(
        review_type=ReviewType.DAILY,
        day=day,
        timezone=TZ,
        period_start=start,
        period_end=start + timedelta(days=1),
        sources=selected,
        collection_status=(status,),
        budget_chars=60_000,
    )


def _payload(**overrides: Any) -> str:
    data: dict[str, Any] = {
        "date": DAY.isoformat(),
        "overview": "One verified run completed.",
        "completed_activities": [
            {"text": "Completed the release check.", "source_refs": ["run-1"]}
        ],
        "knowledge_insights": [
            {"text": "The run produced a report.", "source_refs": ["run-1"]}
        ],
        "problems": [],
        "recommendations": [{"text": "Review the report tomorrow.", "source_refs": []}],
        "source_refs": ["run-1"],
        "confidence_notes": ["Based on one completed run."],
    }
    data.update(overrides)
    return json.dumps(data)


class FakeCollector:
    def __init__(self, review_input: ReviewInput) -> None:
        self.review_input = review_input
        self.calls = 0
        self.cutoffs: list[Any] = []

    def collect_daily(
        self, day: date, timezone: str, *, cutoff: Any = None
    ) -> ReviewInput:
        self.calls += 1
        self.cutoffs.append(cutoff)
        return self.review_input


class FakeLLM:
    def __init__(
        self,
        responses: list[LLMResponse] | None = None,
        *,
        error: Exception | None = None,
        delay: float = 0.0,
        gate: asyncio.Event | None = None,
    ) -> None:
        self.responses = list(responses or [LLMResponse(content=_payload())])
        self.error = error
        self.delay = delay
        self.gate = gate
        self.calls: list[tuple[list[Message], list[dict[str, Any]] | None]] = []
        self.started = asyncio.Event()

    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        self.calls.append((messages, tools))
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        if not self.responses:
            raise RuntimeError("no scripted response")
        return self.responses.pop(0)


def _service(
    tmp_path: Path,
    *,
    llm: FakeLLM | None = None,
    review_input: ReviewInput | None = None,
    timeout_seconds: float = 5.0,
    sensitive_values: tuple[str, ...] = (),
) -> DailyReviewService:
    return DailyReviewService(
        store=ReviewStore(tmp_path / "knowledge"),
        collector=FakeCollector(review_input or _input()),
        llm=llm,
        model_id=MODEL_ID,
        timeout_seconds=timeout_seconds,
        sensitive_values=sensitive_values,
    )


def test_valid_report_is_persisted_with_citations_and_no_tools(tmp_path: Path) -> None:
    async def scenario() -> None:
        llm = FakeLLM(
            [
                LLMResponse(
                    content=_payload(
                        overview="A run completed with token sekret.",
                    )
                )
            ]
        )
        service = _service(
            tmp_path,
            llm=llm,
            sensitive_values=("sekret",),
        )
        try:
            created = await service.generate_daily_review(DAY, "request-1", timezone=TZ)
            assert created.status in {ReviewStatus.PENDING, ReviewStatus.GENERATING}
            stored = await service.wait_for_review(created.review_id)
            assert stored.status is ReviewStatus.COMPLETED
            assert stored.summary is not None
            assert stored.summary.completed_activities[0].source_refs == ("run-1",)
            assert stored.summary.source_refs == ("run-1",)
            assert stored.summary.overview == "A run completed with token [redacted]."
            assert stored.summary_markdown is not None
            assert "run-1" in stored.summary_markdown
            assert stored.model_id == MODEL_ID
            assert stored.generation_attempts == 1
            assert stored.completed_at is not None
            assert stored.source_manifest[0].source_id == "run-1"
            assert stored.input_fingerprint == _input().input_fingerprint

            assert len(llm.calls) == 1
            messages, tools = llm.calls[0]
            assert tools is None
            assert messages[0].role == "system"
            assert "You have no tools" in (messages[0].content or "")
            assert "may_prove_completion=true represents operator acceptance" in (
                messages[0].content or ""
            )
            user = messages[1].content or ""
            assert "run-1" in user
            assert "may_prove_completion" in user
            assert "sekret" not in user
        finally:
            await service.close()

    asyncio.run(scenario())


def test_real_clip_flows_from_collector_to_cited_report(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        knowledge = KnowledgeStore(tmp_path / "knowledge")
        clip, _ = knowledge.create(
            "SQLite WAL reset notes",
            source="web",
            tags=["database"],
            request_id="real-clip",
        )
        with sqlite3.connect(knowledge.path) as connection:
            connection.execute(
                "UPDATE clips SET created_at = ?, updated_at = ? WHERE clip_id = ?",
                (
                    "2026-01-15T02:00:00+00:00",
                    "2026-01-15T02:00:00+00:00",
                    clip.clip_id,
                ),
            )
        payload = _payload(
            overview="A SQLite note was captured.",
            completed_activities=[],
            knowledge_insights=[
                {"text": "Studied SQLite WAL resets.", "source_refs": [clip.clip_id]}
            ],
            source_refs=[clip.clip_id],
        )
        llm = FakeLLM([LLMResponse(content=payload)])
        service = DailyReviewService(
            store=ReviewStore(knowledge),
            collector=ReviewSourceCollector(
                knowledge=knowledge,
                workspace=workspace,
            ),
            llm=llm,
            model_id=MODEL_ID,
        )
        try:
            created = await service.generate_daily_review(
                DAY, "real-clip-request", timezone=TZ
            )
            stored = await service.wait_for_review(created.review_id)
            assert stored.status is ReviewStatus.COMPLETED
            assert stored.summary is not None
            assert stored.summary.knowledge_insights[0].source_refs == (clip.clip_id,)
            assert stored.source_manifest[0].source_id == clip.clip_id
            assert stored.source_manifest[0].content_hash == clip.content_hash
            clip_status = next(
                status
                for status in stored.collection_status
                if status.source_type == "knowledge_clip"
            )
            assert clip_status.status is SourceCollectionState.OK

            user = llm.calls[0][0][1].content or ""
            assert clip.clip_id in user
            assert "SQLite WAL reset notes" in user
        finally:
            await service.close()

    asyncio.run(scenario())


def test_report_source_snapshots_survive_clip_edits_and_archiving(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        knowledge = KnowledgeStore(tmp_path / "knowledge")
        clip, _ = knowledge.create(
            "original wording",
            source="web",
            tags=["test"],
            request_id="snapshot-clip",
        )
        with sqlite3.connect(knowledge.path) as connection:
            connection.execute(
                "UPDATE clips SET created_at = ?, updated_at = ? WHERE clip_id = ?",
                (
                    "2026-01-15T02:00:00+00:00",
                    "2026-01-15T02:00:00+00:00",
                    clip.clip_id,
                ),
            )

        def clip_payload(overview: str) -> str:
            return _payload(
                overview=overview,
                completed_activities=[],
                knowledge_insights=[
                    {"text": "Captured a note.", "source_refs": [clip.clip_id]}
                ],
                source_refs=[clip.clip_id],
            )

        llm = FakeLLM(
            [
                LLMResponse(content=clip_payload("Original report.")),
                LLMResponse(content=clip_payload("Edited report.")),
            ]
        )
        service = DailyReviewService(
            store=ReviewStore(knowledge),
            collector=ReviewSourceCollector(
                knowledge=knowledge,
                workspace=workspace,
            ),
            llm=llm,
            model_id=MODEL_ID,
        )
        try:
            first = await service.generate_daily_review(
                DAY, "snapshot-request", timezone=TZ
            )
            completed = await service.wait_for_review(first.review_id)
            assert completed.source_snapshots[0].source_id == clip.clip_id
            assert "original wording" in completed.source_snapshots[0].snapshot
            original_snapshot = completed.source_snapshots[0].snapshot

            knowledge.update(clip.clip_id, content="edited wording")
            with sqlite3.connect(knowledge.path) as connection:
                connection.execute(
                    "UPDATE clips SET updated_at = ? WHERE clip_id = ?",
                    ("2026-01-15T02:00:00+00:00", clip.clip_id),
                )
            replay = await service.generate_daily_review(
                DAY, "snapshot-request", timezone=TZ
            )
            assert replay.review_id == completed.review_id
            replayed = await service.get_review(completed.review_id)
            assert replayed is not None
            assert replayed.source_snapshots[0].snapshot == original_snapshot
            assert "edited wording" not in replayed.source_snapshots[0].snapshot

            regenerated = await service.generate_daily_review(
                DAY, "snapshot-regenerate", timezone=TZ, regenerate=True
            )
            newer = await service.wait_for_review(regenerated.review_id)
            assert newer.status is ReviewStatus.COMPLETED
            assert "edited wording" in newer.source_snapshots[0].snapshot
            old = await service.get_review(completed.review_id)
            assert old is not None
            assert old.source_snapshots[0].snapshot == original_snapshot

            knowledge.set_archived(clip.clip_id, archived=True)
            archived = await service.generate_daily_review(
                DAY, "snapshot-archived", timezone=TZ, regenerate=True
            )
            archived = await service.wait_for_review(archived.review_id)
            assert archived.status is ReviewStatus.COMPLETED
            assert archived.summary is not None
            assert archived.summary.overview in NO_SOURCE_MESSAGES
            assert archived.source_snapshots == ()
            unchanged = await service.get_review(completed.review_id)
            assert unchanged is not None
            assert unchanged.source_snapshots[0].snapshot == original_snapshot
            assert len(llm.calls) == 2
        finally:
            await service.close()

    asyncio.run(scenario())


def test_budget_omitted_sources_are_not_reported_as_an_empty_day(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source = _source().model_copy(
            update={
                "included": False,
                "omitted_reason": "input_budget_exhausted",
                "snapshot": "",
            }
        )
        llm = FakeLLM(error=AssertionError("model must not be called"))
        service = _service(
            tmp_path,
            llm=llm,
            review_input=_input(sources=(source,)),
        )
        try:
            stored = await service.generate_daily_review(
                DAY, "budget-omitted", timezone=TZ
            )
            assert stored.status is ReviewStatus.COMPLETED
            assert stored.summary is not None
            assert stored.summary.overview not in NO_SOURCE_MESSAGES
            assert "预算" in stored.summary.overview
            assert stored.summary.completed_activities == ()
            assert llm.calls == []
        finally:
            await service.close()

    asyncio.run(scenario())


def test_no_sources_finishes_without_calling_the_model(tmp_path: Path) -> None:
    async def scenario() -> None:
        llm = FakeLLM(error=AssertionError("model must not be called"))
        service = _service(tmp_path, llm=llm, review_input=_input(sources=()))
        try:
            stored = await service.generate_daily_review(DAY, "empty-day", timezone=TZ)
            assert stored.status is ReviewStatus.COMPLETED
            assert stored.summary is not None
            assert stored.summary.overview in NO_SOURCE_MESSAGES
            assert stored.summary.completed_activities == ()
            assert stored.summary.knowledge_insights == ()
            assert stored.summary.problems == ()
            assert stored.summary.recommendations == ()
            assert stored.summary_markdown is not None
            assert stored.summary.overview in stored.summary_markdown
            assert llm.calls == []
        finally:
            await service.close()

    asyncio.run(scenario())


def test_request_id_is_idempotent_and_regeneration_creates_a_version(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        llm = FakeLLM(
            [
                LLMResponse(content=_payload(overview="First report.")),
                LLMResponse(content=_payload(overview="Second report.")),
            ]
        )
        service = _service(tmp_path, llm=llm)
        collector = service.collector
        assert isinstance(collector, FakeCollector)
        try:
            first = await service.generate_daily_review(DAY, "request-1", timezone=TZ)
            first_done = await service.wait_for_review(first.review_id)
            assert first_done.summary is not None

            replay = await service.generate_daily_review(DAY, "request-1", timezone=TZ)
            assert replay.review_id == first_done.review_id
            assert replay.status is ReviewStatus.COMPLETED
            replay_by_new_id = await service.generate_daily_review(
                DAY, "request-2", timezone=TZ
            )
            assert replay_by_new_id.review_id == first_done.review_id
            assert len(llm.calls) == 1

            regenerated = await service.generate_daily_review(
                DAY, "request-3", timezone=TZ, regenerate=True
            )
            assert regenerated.review_id != first_done.review_id
            assert regenerated.version == 2
            assert regenerated.supersedes_review_id == first_done.review_id
            second_done = await service.wait_for_review(regenerated.review_id)
            assert second_done.summary is not None
            assert second_done.summary.overview == "Second report."
            old = await service.get_review(first_done.review_id)
            assert old is not None and old.summary is not None
            assert old.summary.overview == "First report."
            assert len(llm.calls) == 2

            collector.review_input = _input(
                sources=(_source(snapshot='{"run_id":"run-1","status":"changed"}'),)
            )
            with pytest.raises(ReviewPeriodCompleted):
                await service.generate_daily_review(DAY, "request-4", timezone=TZ)
            retried = await service.generate_daily_review(DAY, "request-1", timezone=TZ)
            assert retried.review_id == first_done.review_id
            assert retried.status is ReviewStatus.COMPLETED
            assert len(llm.calls) == 2
        finally:
            await service.close()

    asyncio.run(scenario())


def test_lost_response_retry_returns_the_accepted_review_after_clip_changes(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        knowledge = KnowledgeStore(tmp_path / "knowledge")
        clip, _ = knowledge.create(
            "first clip text",
            source="web",
            tags=["test"],
            request_id="first-clip",
        )
        with sqlite3.connect(knowledge.path) as connection:
            connection.execute(
                "UPDATE clips SET created_at = ?, updated_at = ? WHERE clip_id = ?",
                (
                    "2026-01-15T02:00:00+00:00",
                    "2026-01-15T02:00:00+00:00",
                    clip.clip_id,
                ),
            )
        payload = _payload(
            overview="The first clip was captured.",
            completed_activities=[],
            knowledge_insights=[
                {"text": "Captured a note.", "source_refs": [clip.clip_id]}
            ],
            source_refs=[clip.clip_id],
        )
        llm = FakeLLM([LLMResponse(content=payload)])
        service = DailyReviewService(
            store=ReviewStore(knowledge),
            collector=ReviewSourceCollector(
                knowledge=knowledge,
                workspace=workspace,
            ),
            llm=llm,
            model_id=MODEL_ID,
        )
        try:
            first = await service.generate_daily_review(
                DAY, "lost-response", timezone=TZ
            )
            completed = await service.wait_for_review(first.review_id)
            assert completed.status is ReviewStatus.COMPLETED

            # Simulate a client that never saw the 202 response and retries
            # after the day's sources changed.
            knowledge.create(
                "second clip text",
                source="web",
                tags=["test"],
                request_id="second-clip",
            )
            retried = await service.generate_daily_review(
                DAY, "lost-response", timezone=TZ
            )
            assert retried.review_id == completed.review_id
            assert retried.status is ReviewStatus.COMPLETED
            assert retried.summary == completed.summary
            assert len(llm.calls) == 1
        finally:
            await service.close()

    asyncio.run(scenario())


def test_concurrent_requests_share_one_generation(tmp_path: Path) -> None:
    async def scenario() -> None:
        gate = asyncio.Event()
        llm = FakeLLM([LLMResponse(content=_payload())], gate=gate)
        service = _service(tmp_path, llm=llm)
        try:
            first, second = await asyncio.gather(
                service.generate_daily_review(DAY, "concurrent-a", timezone=TZ),
                service.generate_daily_review(DAY, "concurrent-b", timezone=TZ),
            )
            assert first.review_id == second.review_id
            await asyncio.wait_for(llm.started.wait(), 2)
            assert len(service.store.list_reviews()) == 1
            gate.set()
            settled = await service.wait_for_review(first.review_id)
            assert settled.status is ReviewStatus.COMPLETED
            assert len(llm.calls) == 1
        finally:
            await service.close()

    asyncio.run(scenario())


def test_cancelled_generation_is_recorded_as_failed(tmp_path: Path) -> None:
    async def scenario() -> None:
        gate = asyncio.Event()
        llm = FakeLLM([LLMResponse(content=_payload())], gate=gate)
        service = _service(tmp_path, llm=llm)
        try:
            created = await service.generate_daily_review(DAY, "cancel-me", timezone=TZ)
            await asyncio.wait_for(llm.started.wait(), 2)
            await service.close()
            failed = service.store.get(created.review_id)
            assert failed is not None
            assert failed.status is ReviewStatus.FAILED
            assert failed.error == "generation_cancelled"
            assert failed.summary is None
        finally:
            await service.close()

    asyncio.run(scenario())


def test_close_settles_a_review_before_its_task_starts(tmp_path: Path) -> None:
    async def scenario() -> None:
        llm = FakeLLM([LLMResponse(content=_payload())])
        service = _service(tmp_path, llm=llm)
        created = await service.generate_daily_review(
            DAY, "close-immediately", timezone=TZ
        )
        await service.close()
        failed = service.store.get(created.review_id)
        assert failed is not None
        assert failed.status is ReviewStatus.FAILED
        assert failed.error == "generation_cancelled"
        assert llm.calls == []

    asyncio.run(scenario())


def test_missing_provider_is_an_explicit_failure(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = _service(tmp_path, llm=None)
        try:
            created = await service.generate_daily_review(
                DAY, "no-provider", timezone=TZ
            )
            stored = await service.wait_for_review(created.review_id)
            assert stored.status is ReviewStatus.FAILED
            assert stored.error == "provider_unavailable"
        finally:
            await service.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("name", "llm", "review_input", "error"),
    [
        ("model_error", FakeLLM(error=RuntimeError("boom")), None, "model_error"),
        ("timeout", FakeLLM(delay=1.0), None, "model_timeout"),
        (
            "tool_calls",
            FakeLLM(
                [
                    LLMResponse(
                        content=_payload(),
                        tool_calls=[ToolCall(id="call-1", name="shell", arguments={})],
                    )
                ]
            ),
            None,
            "model_tool_calls_forbidden",
        ),
        ("empty", FakeLLM([LLMResponse(content="   ")]), None, "empty_model_response"),
        (
            "invalid_json",
            FakeLLM([LLMResponse(content="not json")]),
            None,
            "invalid_model_json",
        ),
        (
            "unknown_ref",
            FakeLLM(
                [
                    LLMResponse(
                        content=_payload(
                            knowledge_insights=[
                                {"text": "Unknown.", "source_refs": ["run-missing"]}
                            ]
                        )
                    )
                ]
            ),
            None,
            "invalid_source_reference",
        ),
        (
            "missing_ref",
            FakeLLM(
                [
                    LLMResponse(
                        content=_payload(
                            knowledge_insights=[
                                {"text": "No evidence.", "source_refs": []}
                            ]
                        )
                    )
                ]
            ),
            None,
            "missing_source_reference",
        ),
        (
            "fabricated_completion",
            FakeLLM([LLMResponse(content=_payload())]),
            _input(sources=(_source(may_prove_completion=False),)),
            "fabricated_completion",
        ),
        (
            "wrong_date",
            FakeLLM([LLMResponse(content=_payload(date="2026-01-14"))]),
            None,
            "invalid_model_output",
        ),
        (
            "too_many_items",
            FakeLLM(
                [
                    LLMResponse(
                        content=_payload(
                            completed_activities=[
                                {
                                    "text": f"activity {index}",
                                    "source_refs": ["run-1"],
                                }
                                for index in range(6)
                            ]
                        )
                    )
                ]
            ),
            None,
            "invalid_model_output",
        ),
    ],
)
def test_invalid_model_results_fail_explicitly(
    tmp_path: Path,
    name: str,
    llm: FakeLLM,
    review_input: ReviewInput | None,
    error: str,
) -> None:
    async def scenario() -> None:
        service = _service(
            tmp_path,
            llm=llm,
            review_input=review_input,
            timeout_seconds=0.05,
        )
        try:
            created = await service.generate_daily_review(
                DAY, f"invalid-{name}", timezone=TZ
            )
            stored = await service.wait_for_review(created.review_id)
            assert stored.status is ReviewStatus.FAILED
            assert stored.error == error
            assert stored.summary is None
            assert stored.summary_markdown is None
        finally:
            await service.close()

    asyncio.run(scenario())


def test_completed_report_is_readable_after_restart_and_stale_rows_fail(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        llm = FakeLLM([LLMResponse(content=_payload())])
        service = _service(tmp_path, llm=llm)
        completed = await service.generate_daily_review(DAY, "persisted", timezone=TZ)
        completed = await service.wait_for_review(completed.review_id)
        await service.close()
        assert completed.summary is not None

        stale_store = ReviewStore(tmp_path / "knowledge")
        second_day = date(2026, 1, 16)
        stale_input = _input(day=second_day)
        stale = stale_store.create_or_get(
            request_id="stale",
            review_type=stale_input.review_type,
            day=stale_input.date,
            period_start=stale_input.period_start.isoformat(),
            period_end=stale_input.period_end.isoformat(),
            timezone=stale_input.timezone,
            input_fingerprint=stale_input.input_fingerprint,
            source_manifest=tuple(
                source.manifest_entry() for source in stale_input.sources
            ),
            collection_status=stale_input.collection_status,
        ).review

        restarted = DailyReviewService(
            store=ReviewStore(tmp_path / "knowledge"),
            collector=FakeCollector(_input()),
            llm=None,
            model_id=MODEL_ID,
        )
        await restarted.start()
        try:
            persisted = await restarted.get_review(completed.review_id)
            assert persisted is not None
            assert persisted.status is ReviewStatus.COMPLETED
            assert persisted.summary == completed.summary
            assert persisted.summary_markdown == completed.summary_markdown

            interrupted = restarted.store.get(stale.review_id)
            assert interrupted is not None
            assert interrupted.status is ReviewStatus.FAILED
            assert interrupted.error == "interrupted_by_restart"
        finally:
            await restarted.close()

    asyncio.run(scenario())


def test_scheduled_review_creates_delivery_outbox_on_completion(
    tmp_path: Path,
) -> None:
    from cairn.review.models import ReviewDeliveryStatus

    llm = FakeLLM()
    service = _service(tmp_path, llm=llm)

    async def scenario() -> None:
        review = await service.generate_daily_review(DAY, "host-review", deliver=True)
        settled = await service.wait_for_review(review.review_id)
        assert settled.status is ReviewStatus.COMPLETED
        delivery = service.store.get_delivery(review.review_id)
        assert delivery is not None
        assert delivery.status is ReviewDeliveryStatus.PENDING
        await service.close()

    asyncio.run(scenario())


def test_manual_review_and_no_source_review_delivery_policy(tmp_path: Path) -> None:
    from cairn.review.models import ReviewDeliveryStatus

    llm = FakeLLM()
    service = _service(tmp_path, llm=llm)

    async def scenario() -> None:
        manual = await service.generate_daily_review(DAY, "manual-review")
        await service.wait_for_review(manual.review_id)
        assert service.store.get_delivery(manual.review_id) is None

        await service.close()

    asyncio.run(scenario())

    no_source_service = _service(
        tmp_path / "no-source", llm=FakeLLM(), review_input=_input(sources=())
    )

    async def no_source() -> None:
        review = await no_source_service.generate_daily_review(
            DAY, "host-no-source", deliver=True
        )
        assert review.status is ReviewStatus.COMPLETED
        delivery = no_source_service.store.get_delivery(review.review_id)
        assert delivery is not None
        assert delivery.status is ReviewDeliveryStatus.PENDING
        await no_source_service.close()

    asyncio.run(no_source())


def test_cutoff_is_forwarded_to_the_collector(tmp_path: Path) -> None:
    from datetime import time

    llm = FakeLLM()
    collector = FakeCollector(_input())
    service = DailyReviewService(
        store=ReviewStore(tmp_path / "knowledge"),
        collector=collector,
        llm=llm,
        model_id=MODEL_ID,
    )

    async def scenario() -> None:
        review = await service.generate_daily_review(
            DAY, "cutoff-review", cutoff=time(20, 30)
        )
        await service.wait_for_review(review.review_id)
        assert collector.cutoffs == [time(20, 30)]
        await service.close()

    asyncio.run(scenario())
