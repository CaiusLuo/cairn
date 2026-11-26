"""Weekly reviews reuse daily evidence without widening its proof."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from cairn.agent.models.messages import LLMResponse
from cairn.knowledge.models import Clip, content_hash
from cairn.knowledge.store import KnowledgeStore
from cairn.review.collector import WeeklySourceCollector
from cairn.review.models import (
    DailyReview,
    ReviewInput,
    ReviewItem,
    ReviewStatus,
    ReviewType,
    SourceCollectionState,
    SourceCollectionStatus,
    SourceManifestEntry,
    SourceSnapshot,
    SourceTrust,
    WeeklyReview,
)
from cairn.review.service import ReviewNoContent
from cairn.review.store import ReviewPeriodCompleted, ReviewStore
from cairn.review.weekly import (
    ReviewPeriodNotEnded,
    WeeklyReviewService,
    iso_week_id,
    week_period,
    week_period_for_end,
    week_start,
)
from tests.support.pg import database_for

TZ = "Asia/Shanghai"
WEEK = "2026-W03"
MODEL_ID = "weekly-test-model"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _task_source(source_id: str, *, completed: bool = True) -> SourceSnapshot:
    snapshot = _json({"task_id": source_id, "status": "done"})
    return SourceSnapshot(
        source_type="development_task",
        source_id=source_id,
        content_hash=_hash(snapshot),
        occurred_at="2026-01-14T10:00:00+08:00",
        trust=SourceTrust.SYSTEM_RECEIPT,
        may_prove_completion=completed,
        verification="business_status=done",
        snapshot=snapshot,
    )


def _clip_source(source_id: str) -> SourceSnapshot:
    snapshot = _json({"clip_id": source_id})
    return SourceSnapshot(
        source_type="knowledge_clip",
        source_id=source_id,
        content_hash=_hash(snapshot),
        occurred_at="2026-01-14T11:00:00+08:00",
        trust=SourceTrust.USER_PROVIDED,
        snapshot=snapshot,
    )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _seed_daily(
    store: ReviewStore,
    day: date,
    sources: tuple[SourceSnapshot, ...],
    *,
    version: int = 1,
    summary: DailyReview | None = None,
) -> str:
    zone = ZoneInfo(TZ)
    start = datetime.combine(day - timedelta(days=1), time(20, 30), tzinfo=zone)
    end = datetime.combine(day, time(20, 30), tzinfo=zone)
    result = store.create_or_get(
        request_id=f"daily-{day.isoformat()}-v{version}",
        review_type=ReviewType.DAILY,
        day=day,
        period_start=start.isoformat(),
        period_end=end.isoformat(),
        timezone=TZ,
        input_fingerprint=_hash(f"input-{day.isoformat()}-{version}"),
        source_manifest=tuple(source.manifest_entry() for source in sources),
        collection_status=(
            SourceCollectionStatus(
                source_type="development_task",
                status=SourceCollectionState.OK,
                discovered=len(sources),
                included=len(sources),
            ),
        ),
        source_snapshots=sources,
        regenerate=version > 1,
    )
    store.mark_generating(result.review.review_id, MODEL_ID)
    store.complete(
        result.review.review_id,
        summary=summary
        or DailyReview(
            date=day,
            overview=f"Daily summary for {day.isoformat()}.",
        ),
        summary_markdown=f"# {day.isoformat()} 日报",
        model_id=MODEL_ID,
    )
    return result.review.review_id


def _task_version(
    source_id: str,
    status: str,
    *,
    may_prove_completion: bool,
    occurred_at: str,
) -> SourceSnapshot:
    snapshot = _json({"task_id": source_id, "status": status})
    return SourceSnapshot(
        source_type="development_task",
        source_id=source_id,
        content_hash=_hash(snapshot),
        occurred_at=occurred_at,
        trust=SourceTrust.SYSTEM_RECEIPT,
        may_prove_completion=may_prove_completion,
        verification=f"business_status={status}",
        snapshot=snapshot,
    )


def _seed_manifest_daily(
    store: ReviewStore,
    day: date,
    entry: SourceManifestEntry,
) -> str:
    manifest = entry.model_copy(
        update={
            "included": False,
            "omitted_reason": "input_budget_exhausted",
        }
    )
    zone = ZoneInfo(TZ)
    start = datetime.combine(day - timedelta(days=1), time(20, 30), tzinfo=zone)
    end = datetime.combine(day, time(20, 30), tzinfo=zone)
    result = store.create_or_get(
        request_id=f"daily-manifest-{day.isoformat()}",
        review_type=ReviewType.DAILY,
        day=day,
        period_start=start.isoformat(),
        period_end=end.isoformat(),
        timezone=TZ,
        input_fingerprint=_hash(f"manifest-{day.isoformat()}"),
        source_manifest=(manifest,),
        collection_status=(
            SourceCollectionStatus(
                source_type=manifest.source_type,
                status=SourceCollectionState.PARTIAL,
                discovered=1,
                included=0,
                omitted=1,
            ),
        ),
        source_snapshots=(),
    )
    store.mark_generating(result.review.review_id, MODEL_ID)
    store.complete(
        result.review.review_id,
        summary=DailyReview(
            date=day,
            overview=f"Daily summary for {day.isoformat()}.",
        ),
        summary_markdown=f"# {day.isoformat()} 日报",
        model_id=MODEL_ID,
    )
    return result.review.review_id


class FakeKnowledge:
    """Only the read projection the collector needs, with a fixed window."""

    def __init__(self, clips: tuple[Clip, ...] = ()) -> None:
        self.clips = clips
        self.windows: list[tuple[str, str]] = []

    def list_clips_touched_between(
        self, start: str, end: str, *, include_archived: bool = False
    ) -> tuple[Clip, ...]:
        self.windows.append((start, end))
        return tuple(clip for clip in self.clips if start <= clip.updated_at < end)


def _collector(
    tmp_path: Path, store: ReviewStore, *, knowledge: FakeKnowledge | None = None
) -> WeeklySourceCollector:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return WeeklySourceCollector(
        reviews=store,
        knowledge=knowledge or FakeKnowledge(),  # type: ignore[arg-type]
        workspace=workspace,
        max_chars=200_000,
    )


def test_week_period_covers_seven_daily_windows_and_crosses_years() -> None:
    period = week_period(WEEK, timezone=TZ)
    assert period.day == date(2026, 1, 18)
    assert period.period_start.isoformat() == "2026-01-11T20:30:00+08:00"
    assert period.period_end.isoformat() == "2026-01-18T20:30:00+08:00"
    assert period.period_end - period.period_start == timedelta(days=7)

    cross_year = week_period("2026-W01", timezone=TZ)
    assert cross_year.period_start.isoformat() == "2025-12-28T20:30:00+08:00"
    assert iso_week_id(date(2026, 1, 1)) == "2026-W01"
    assert week_start("2026-W01") == date(2026, 1, 4)

    derived = week_period_for_end(
        datetime(2026, 1, 18, 20, 40, tzinfo=ZoneInfo(TZ)), timezone=TZ
    )
    assert derived == period
    with pytest.raises(ValueError):
        week_period("2026-W54", timezone=TZ)


def test_weekly_collector_accepts_a_dst_transition_week(tmp_path: Path) -> None:
    # US DST starts on 2026-03-08: the local week is seven calendar days but
    # only 167 absolute hours, which must not be mistaken for a short period.
    zone = "America/New_York"
    period = week_period("2026-W10", timezone=zone)
    span = period.period_end.astimezone(UTC) - period.period_start.astimezone(UTC)
    assert span == timedelta(hours=167)
    collector = _collector(
        tmp_path, ReviewStore(KnowledgeStore(database_for(tmp_path)))
    )
    review_input = collector.collect_weekly(period, zone)
    assert review_input.period_start == period.period_start
    assert review_input.period_end == period.period_end
    assert review_input.collection_status[0].notes
    with pytest.raises(ValueError):
        week_period("not-a-week", timezone=TZ)


def test_collector_counts_each_daily_once_and_dedupes_original_sources(
    tmp_path: Path,
) -> None:
    period = week_period(WEEK, timezone=TZ)
    store = ReviewStore(KnowledgeStore(database_for(tmp_path)))
    shared = _task_source("task-1")
    end_days = [
        (period.period_start + timedelta(days=offset + 1)).date() for offset in range(7)
    ]
    missing_day = end_days[3]
    first_version_id = ""
    latest_version_id = ""
    for index, day in enumerate(end_days):
        if day == missing_day:
            continue
        if index == 0:
            first_version_id = _seed_daily(store, day, (shared, _clip_source("clip-0")))
            latest_version_id = _seed_daily(
                store, day, (shared, _clip_source("clip-0")), version=2
            )
            continue
        _seed_daily(
            store, day, (_task_source(f"task-{index}"), _clip_source(f"clip-{index}"))
        )

    missing_start = (period.period_start + timedelta(days=3)).astimezone(UTC)
    raw_content = "Raw record for the day without a daily review."
    raw_clip = Clip(
        clip_id="clip-raw-missing",
        content=raw_content,
        source="web",
        content_hash=content_hash(raw_content),
        created_at=missing_start.isoformat(),
        updated_at=missing_start.isoformat(),
    )
    knowledge = FakeKnowledge((raw_clip,))
    review_input = _collector(tmp_path, store, knowledge=knowledge).collect_weekly(
        period, TZ
    )

    assert review_input.review_type is ReviewType.WEEKLY
    daily = [s for s in review_input.sources if s.source_type == "daily_review"]
    assert len(daily) == 6
    assert len({source.source_id for source in daily}) == 6
    assert first_version_id not in {source.source_id for source in daily}
    assert latest_version_id in {source.source_id for source in daily}

    tasks = [s for s in review_input.sources if s.source_id == "task-1"]
    assert len(tasks) == 1
    assert tasks[0].content_hash == shared.content_hash
    assert "clip-raw-missing" in {s.source_id for s in review_input.sources}
    assert "task-1" in review_input.completion_source_ids
    assert "daily_review" not in review_input.completion_source_ids

    status = next(
        item
        for item in review_input.collection_status
        if item.source_type == "daily_review"
    )
    assert status.status is SourceCollectionState.PARTIAL
    assert (status.discovered, status.included, status.omitted) == (7, 6, 1)
    assert any(missing_day.isoformat() in note for note in status.notes)
    assert any("去重" in note for note in status.notes)


def test_weekly_daily_source_carries_conversation_and_recall(
    tmp_path: Path,
) -> None:
    period = week_period(WEEK, timezone=TZ)
    store = ReviewStore(KnowledgeStore(database_for(tmp_path)))
    day = (period.period_start + timedelta(days=1)).date()
    summary = DailyReview(
        date=day,
        overview="A day with a valuable conversation.",
        conversation_highlights=(
            ReviewItem(text="Chose the nightly backup window.", source_refs=("run-1",)),
        ),
        knowledge_recall=(
            ReviewItem(
                text="The old runbook documents pg_dump.", source_refs=("recall-1",)
            ),
        ),
    )
    _seed_daily(store, day, (_task_source("run-1"),), summary=summary)

    review_input = _collector(tmp_path, store).collect_weekly(period, TZ)
    daily = next(
        source
        for source in review_input.sources
        if source.source_type == "daily_review"
    )
    payload = json.loads(daily.snapshot)
    assert payload["conversation_highlights"] == [
        {"text": "Chose the nightly backup window.", "source_refs": ["run-1"]}
    ]
    assert payload["knowledge_recall"] == [
        {"text": "The old runbook documents pg_dump.", "source_refs": ["recall-1"]}
    ]
    assert daily.may_prove_completion is False


def test_budget_omitted_manifest_identity_is_not_completion_evidence(
    tmp_path: Path,
) -> None:
    period = week_period(WEEK, timezone=TZ)
    store = ReviewStore(KnowledgeStore(database_for(tmp_path)))
    day = (period.period_start + timedelta(days=1)).date()
    entry = _task_source("task-omitted").manifest_entry()
    _seed_manifest_daily(store, day, entry)

    review_input = _collector(tmp_path, store).collect_weekly(period, TZ)
    source = next(
        item for item in review_input.sources if item.source_id == "task-omitted"
    )
    assert source.included is False
    assert source.may_prove_completion is False
    assert source.omitted_reason == "input_budget_exhausted"
    assert "identity_only" in source.verification
    assert "task-omitted" not in review_input.completion_source_ids
    assert "task-omitted" not in review_input.included_source_ids
    daily_id = next(
        item.source_id
        for item in review_input.included_sources
        if item.source_type == "daily_review"
    )
    status = next(
        item
        for item in review_input.collection_status
        if item.source_type == "development_task"
    )
    assert (status.discovered, status.included, status.omitted) == (1, 0, 1)

    payload = _payload(
        period,
        major_events=[{"text": "Completed it.", "source_refs": [daily_id]}],
        progress=[{"text": "Finished it.", "source_refs": [daily_id]}],
        source_refs=[daily_id],
    )
    service, _ = _service(tmp_path, review_input, llm=FakeLLM(payload))

    async def scenario() -> Any:
        created = await service.generate_weekly_review(
            WEEK, "weekly-omitted", timezone=TZ
        )
        settled = await service.wait_for_review(created.review_id)
        await service.close()
        return settled

    settled = asyncio.run(scenario())
    assert settled.status is ReviewStatus.FAILED
    assert settled.error == "fabricated_completion"


def test_weekly_collector_keeps_the_latest_task_state_without_double_counting(
    tmp_path: Path,
) -> None:
    period = week_period(WEEK, timezone=TZ)
    store = ReviewStore(KnowledgeStore(database_for(tmp_path)))
    end_days = [
        (period.period_start + timedelta(days=offset + 1)).date() for offset in range(7)
    ]
    in_progress = _task_version(
        "task-evolve",
        "in_progress",
        may_prove_completion=False,
        occurred_at="2026-01-12T10:00:00+08:00",
    )
    testing = _task_version(
        "task-evolve",
        "testing",
        may_prove_completion=False,
        occurred_at="2026-01-14T10:00:00+08:00",
    )
    done = _task_version(
        "task-evolve",
        "done",
        may_prove_completion=True,
        occurred_at="2026-01-17T10:00:00+08:00",
    )
    _seed_daily(store, end_days[0], (in_progress,))
    _seed_daily(store, end_days[2], (testing,))
    _seed_daily(store, end_days[5], (done,))

    review_input = _collector(tmp_path, store).collect_weekly(period, TZ)
    tasks = [item for item in review_input.sources if item.source_id == "task-evolve"]
    assert len(tasks) == 1
    assert tasks[0].content_hash == done.content_hash
    assert tasks[0].may_prove_completion is True
    assert "task-evolve" in review_input.completion_source_ids
    assert "task-evolve" in review_input.included_source_ids
    history = json.loads(tasks[0].snapshot)["observed_versions"]
    assert [item["content_hash"] for item in history] == [
        in_progress.content_hash,
        testing.content_hash,
        done.content_hash,
    ]
    status = next(
        item
        for item in review_input.collection_status
        if item.source_type == "development_task"
    )
    assert status.discovered == 1
    assert any(
        "变化" in note for item in review_input.collection_status for note in item.notes
    )


def test_weekly_collector_never_reuses_an_older_done_after_reopen(
    tmp_path: Path,
) -> None:
    period = week_period(WEEK, timezone=TZ)
    store = ReviewStore(KnowledgeStore(database_for(tmp_path)))
    end_days = [
        (period.period_start + timedelta(days=offset + 1)).date() for offset in range(7)
    ]
    done = _task_version(
        "task-reopened",
        "done",
        may_prove_completion=True,
        occurred_at="2026-01-12T10:00:00+08:00",
    )
    reopened = _task_version(
        "task-reopened",
        "in_progress",
        may_prove_completion=False,
        occurred_at="2026-01-14T10:00:00+08:00",
    )
    _seed_daily(store, end_days[0], (done,))
    _seed_daily(store, end_days[2], (reopened,))

    review_input = _collector(tmp_path, store).collect_weekly(period, TZ)
    task = next(
        item for item in review_input.sources if item.source_id == "task-reopened"
    )
    assert task.content_hash == reopened.content_hash
    assert task.may_prove_completion is False
    assert "task-reopened" not in review_input.completion_source_ids


def test_weekly_collector_uses_the_last_occurrence_when_a_hash_repeats(
    tmp_path: Path,
) -> None:
    period = week_period(WEEK, timezone=TZ)
    store = ReviewStore(KnowledgeStore(database_for(tmp_path)))
    end_days = [
        (period.period_start + timedelta(days=offset + 1)).date() for offset in range(7)
    ]
    done = _task_version(
        "task-aba",
        "done",
        may_prove_completion=True,
        occurred_at="2026-01-12T10:00:00+08:00",
    )
    reopened = _task_version(
        "task-aba",
        "in_progress",
        may_prove_completion=False,
        occurred_at="2026-01-14T10:00:00+08:00",
    )
    _seed_daily(store, end_days[0], (done,))
    _seed_daily(store, end_days[2], (reopened,))
    _seed_daily(store, end_days[5], (done,))

    review_input = _collector(tmp_path, store).collect_weekly(period, TZ)
    task = next(item for item in review_input.sources if item.source_id == "task-aba")
    assert task.content_hash == done.content_hash
    assert task.may_prove_completion is True
    assert "task-aba" in review_input.completion_source_ids


def test_identity_only_week_creates_no_review(tmp_path: Path) -> None:
    period = week_period(WEEK, timezone=TZ)
    source = WeeklySourceCollector._identity_source(
        _task_source("task-identity").manifest_entry()
    )
    llm = FakeLLM()
    service, _ = _service(tmp_path, _weekly_input(period, (source,)), llm=llm)

    async def scenario() -> Any:
        try:
            with pytest.raises(ReviewNoContent):
                await service.generate_weekly_review(
                    WEEK, "weekly-identity", timezone=TZ
                )
            return await service.list_reviews(review_type=ReviewType.WEEKLY)
        finally:
            await service.close()

    assert asyncio.run(scenario()) == ()
    assert llm.calls == []


class FakeWeeklyCollector:
    def __init__(self, review_input: ReviewInput) -> None:
        self.review_input = review_input
        self.periods: list[Any] = []

    def collect_weekly(self, period: Any, timezone: str) -> ReviewInput:
        self.periods.append((period, timezone))
        return self.review_input


class FakeLLM:
    def __init__(self, content: str | None = None) -> None:
        self.content = content or ""
        self.calls: list[tuple[object, object]] = []

    async def generate(self, messages: object, tools: object = None) -> LLMResponse:
        self.calls.append((messages, tools))
        return LLMResponse(content=self.content)


def _weekly_input(period: Any, sources: tuple[SourceSnapshot, ...]) -> ReviewInput:
    return ReviewInput.build(
        review_type=ReviewType.WEEKLY,
        day=period.day,
        timezone=TZ,
        period_start=period.period_start,
        period_end=period.period_end,
        sources=sources,
        collection_status=(
            SourceCollectionStatus(
                source_type="development_task",
                status=SourceCollectionState.OK,
                discovered=len(sources),
                included=len(sources),
            ),
        ),
        budget_chars=200_000,
    )


def _payload(period: Any, **overrides: Any) -> str:
    data: dict[str, Any] = {
        "week_id": period.week_id,
        "period_start": period.period_start.isoformat(),
        "period_end": period.period_end.isoformat(),
        "overview": "A verified week.",
        "major_events": [{"text": "Shipped one task.", "source_refs": ["task-1"]}],
        "progress": [{"text": "Finished the task.", "source_refs": ["task-1"]}],
        "knowledge_insights": [],
        "recurring_problems": [],
        "next_week_priorities": [{"text": "Continue.", "source_refs": []}],
        "source_refs": ["task-1"],
        "confidence_notes": [],
    }
    data.update(overrides)
    return json.dumps(data)


def _service(
    tmp_path: Path,
    review_input: ReviewInput,
    *,
    llm: FakeLLM | None = None,
    clock: Any = None,
) -> tuple[WeeklyReviewService, FakeWeeklyCollector]:
    collector = FakeWeeklyCollector(review_input)
    kwargs: dict[str, Any] = {}
    if clock is not None:
        kwargs["clock"] = clock
    service = WeeklyReviewService(
        store=ReviewStore(KnowledgeStore(database_for(tmp_path))),
        collector=collector,
        llm=llm or FakeLLM(_payload(week_period(WEEK, timezone=TZ))),
        model_id=MODEL_ID,
        timeout_seconds=5.0,
        **kwargs,
    )
    return service, collector


def test_weekly_service_persists_a_cited_report_and_never_enables_tools(
    tmp_path: Path,
) -> None:
    period = week_period(WEEK, timezone=TZ)
    source = _task_source("task-1")
    llm = FakeLLM(_payload(period))
    service, collector = _service(tmp_path, _weekly_input(period, (source,)), llm=llm)

    async def scenario() -> tuple[Any, Any]:
        created = await service.generate_weekly_review(WEEK, "weekly-1", timezone=TZ)
        settled = await service.wait_for_review(created.review_id)
        await service.close()
        return created, settled

    created, settled = asyncio.run(scenario())
    assert created.review_type is ReviewType.WEEKLY
    assert settled.status is ReviewStatus.COMPLETED
    assert isinstance(settled.summary, WeeklyReview)
    assert settled.summary.week_id == WEEK
    assert settled.summary.period_start == period.period_start
    assert settled.summary.period_end == period.period_end
    assert settled.summary.progress[0].source_refs == ("task-1",)
    assert settled.summary_markdown is not None
    assert "覆盖: 2026-01-11T20:30:00+08:00" in settled.summary_markdown
    assert "2026-01-18T20:30:00+08:00" in settled.summary_markdown
    assert len(settled.source_snapshots) == 1
    assert collector.periods[0][0] == period
    assert llm.calls and llm.calls[0][1] is None

    replay = asyncio.run(
        WeeklyReviewService(
            store=ReviewStore(KnowledgeStore(database_for(tmp_path))),
            collector=collector,
            llm=llm,
            model_id=MODEL_ID,
        ).generate_weekly_review(WEEK, "weekly-1", timezone=TZ)
    )
    assert replay.review_id == settled.review_id
    assert replay.version == 1


def test_weekly_conversation_highlights_and_recall_are_cited(
    tmp_path: Path,
) -> None:
    period = week_period(WEEK, timezone=TZ)
    source = _task_source("task-1")
    payload = _payload(
        period,
        conversation_highlights=[
            {"text": "Agreed on the release checklist.", "source_refs": ["task-1"]}
        ],
        knowledge_recall=[
            {"text": "The runbook explains the rollback.", "source_refs": ["task-1"]}
        ],
    )
    service, _ = _service(
        tmp_path, _weekly_input(period, (source,)), llm=FakeLLM(payload)
    )

    async def scenario() -> Any:
        created = await service.generate_weekly_review(
            WEEK, "weekly-conversation-recall", timezone=TZ
        )
        settled = await service.wait_for_review(created.review_id)
        await service.close()
        return settled

    settled = asyncio.run(scenario())
    assert settled.status is ReviewStatus.COMPLETED
    assert settled.summary is not None
    assert settled.summary.conversation_highlights[0].source_refs == ("task-1",)
    assert settled.summary.knowledge_recall[0].source_refs == ("task-1",)
    markdown = settled.summary_markdown or ""
    assert "## 对话亮点" in markdown
    assert "## 知识召回" in markdown
    assert "Agreed on the release checklist." in markdown
    assert "The runbook explains the rollback." in markdown


def test_progress_without_original_completion_evidence_fails(tmp_path: Path) -> None:
    period = week_period(WEEK, timezone=TZ)
    daily = SourceSnapshot(
        source_type="daily_review",
        source_id="review-daily",
        content_hash=_hash("daily"),
        occurred_at=period.period_end.isoformat(),
        trust=SourceTrust.AGENT_INFERRED,
        may_prove_completion=False,
        verification="daily_review_summary",
        snapshot='{"overview":"daily"}',
    )
    payload = _payload(
        period,
        progress=[{"text": "Claimed completion.", "source_refs": ["review-daily"]}],
        source_refs=["review-daily"],
        major_events=[
            {"text": "A day was summarized.", "source_refs": ["review-daily"]}
        ],
    )
    service, _ = _service(
        tmp_path,
        _weekly_input(period, (daily,)),
        llm=FakeLLM(payload),
    )

    async def scenario() -> Any:
        created = await service.generate_weekly_review(
            WEEK, "weekly-fabricated", timezone=TZ
        )
        settled = await service.wait_for_review(created.review_id)
        await service.close()
        return settled

    settled = asyncio.run(scenario())
    assert settled.status is ReviewStatus.FAILED
    assert settled.error == "fabricated_completion"


def test_week_without_sources_creates_no_review(tmp_path: Path) -> None:
    period = week_period(WEEK, timezone=TZ)
    llm = FakeLLM()
    service, _ = _service(tmp_path, _weekly_input(period, ()), llm=llm)

    async def scenario() -> Any:
        try:
            with pytest.raises(ReviewNoContent):
                await service.generate_weekly_review(WEEK, "weekly-empty", timezone=TZ)
            return await service.list_reviews(review_type=ReviewType.WEEKLY)
        finally:
            await service.close()

    assert asyncio.run(scenario()) == ()
    assert llm.calls == []


def test_weekly_review_rejects_a_period_before_its_cutoff(tmp_path: Path) -> None:
    period = week_period(WEEK, timezone=TZ)
    source = _task_source("task-1")
    for index, moment in enumerate(
        (
            datetime(2026, 1, 14, 12, 0, tzinfo=ZoneInfo(TZ)),
            period.period_end - timedelta(seconds=1),
        )
    ):
        service, _ = _service(
            tmp_path / str(index),
            _weekly_input(period, (source,)),
            clock=lambda moment=moment: moment,
        )

        async def scenario(
            service: WeeklyReviewService = service, index: int = index
        ) -> None:
            with pytest.raises(ReviewPeriodNotEnded):
                await service.generate_weekly_review(
                    WEEK, f"weekly-early-{index}", timezone=TZ
                )
            await service.close()

        asyncio.run(scenario())


def test_weekly_review_accepts_the_cutoff_moment_and_later(tmp_path: Path) -> None:
    period = week_period(WEEK, timezone=TZ)
    for index, moment in enumerate(
        (period.period_end, period.period_end + timedelta(days=3))
    ):
        service, _ = _service(
            tmp_path / str(index),
            _weekly_input(period, (_task_source("task-1"),)),
            clock=lambda moment=moment: moment,
        )

        async def scenario(
            service: WeeklyReviewService = service, index: int = index
        ) -> Any:
            created = await service.generate_weekly_review(
                WEEK, f"weekly-ended-{index}", timezone=TZ
            )
            settled = await service.wait_for_review(created.review_id)
            await service.close()
            return settled

        assert asyncio.run(scenario()).status is ReviewStatus.COMPLETED


def test_weekly_schedule_can_reuse_an_operator_completed_report(
    tmp_path: Path,
) -> None:
    period = week_period(WEEK, timezone=TZ)
    first = _task_source("task-1")
    second = _task_source("task-2")
    service, collector = _service(tmp_path, _weekly_input(period, (first,)))
    llm = service.llm
    assert isinstance(llm, FakeLLM)

    async def scenario() -> tuple[Any, Any]:
        created = await service.generate_weekly_review(
            WEEK, "weekly-manual", timezone=TZ
        )
        await service.wait_for_review(created.review_id)
        collector.review_input = _weekly_input(period, (second,))
        with pytest.raises(ReviewPeriodCompleted):
            await service.generate_weekly_review(WEEK, "weekly-scheduler", timezone=TZ)
        reused = await service.generate_weekly_review(
            WEEK,
            "weekly-scheduler-reuse",
            timezone=TZ,
            reuse_completed=True,
        )
        await service.close()
        return created, reused

    created, reused = asyncio.run(scenario())
    assert reused.review_id == created.review_id
    assert len(llm.calls) == 1


def test_regeneration_creates_a_new_version_and_bad_period_is_rejected(
    tmp_path: Path,
) -> None:
    period = week_period(WEEK, timezone=TZ)
    source = _task_source("task-1")
    llm = FakeLLM(_payload(period))
    service, _ = _service(tmp_path, _weekly_input(period, (source,)), llm=llm)

    async def scenario() -> tuple[Any, Any]:
        first = await service.generate_weekly_review(WEEK, "weekly-a", timezone=TZ)
        await service.wait_for_review(first.review_id)
        second = await service.generate_weekly_review(
            WEEK, "weekly-b", timezone=TZ, regenerate=True
        )
        settled = await service.wait_for_review(second.review_id)
        await service.close()
        return second, settled

    _, settled = asyncio.run(scenario())
    assert settled.status is ReviewStatus.COMPLETED
    assert settled.version == 2
    assert settled.supersedes_review_id is not None

    with pytest.raises(ValueError):
        asyncio.run(
            service.generate_weekly_review(
                WEEK,
                "weekly-mismatch",
                timezone=TZ,
                period_end=datetime(2026, 1, 25, 20, 30, tzinfo=ZoneInfo(TZ)),
            )
        )


def test_weekly_citations_must_reference_supplied_sources(tmp_path: Path) -> None:
    period = week_period(WEEK, timezone=TZ)
    source = _task_source("task-1")
    llm = FakeLLM(
        _payload(
            period,
            major_events=[{"text": "Invented.", "source_refs": ["task-99"]}],
            source_refs=["task-99"],
        )
    )
    service, _ = _service(tmp_path, _weekly_input(period, (source,)), llm=llm)

    async def scenario() -> Any:
        created = await service.generate_weekly_review(
            WEEK, "weekly-invalid-ref", timezone=TZ
        )
        settled = await service.wait_for_review(created.review_id)
        await service.close()
        return settled

    settled = asyncio.run(scenario())
    assert settled.status is ReviewStatus.FAILED
    assert settled.error == "invalid_source_reference"


@pytest.mark.parametrize("field", ["conversation_highlights", "knowledge_recall"])
def test_weekly_new_sections_require_citations(tmp_path: Path, field: str) -> None:
    period = week_period(WEEK, timezone=TZ)
    payload = _payload(period, **{field: [{"text": "No evidence.", "source_refs": []}]})
    service, _ = _service(
        tmp_path,
        _weekly_input(period, (_task_source("task-1"),)),
        llm=FakeLLM(payload),
    )

    async def scenario() -> Any:
        created = await service.generate_weekly_review(
            WEEK, f"weekly-missing-{field}", timezone=TZ
        )
        settled = await service.wait_for_review(created.review_id)
        await service.close()
        return settled

    settled = asyncio.run(scenario())
    assert settled.status is ReviewStatus.FAILED
    assert settled.error == "missing_source_reference"


def test_weekly_service_rejects_a_week_mismatch_from_the_model(
    tmp_path: Path,
) -> None:
    period = week_period(WEEK, timezone=TZ)
    payload = _payload(period, week_id="2026-W04")
    service, _ = _service(
        tmp_path,
        _weekly_input(period, (_task_source("task-1"),)),
        llm=FakeLLM(payload),
    )

    async def scenario() -> Any:
        created = await service.generate_weekly_review(
            WEEK, "weekly-model-mismatch", timezone=TZ
        )
        settled = await service.wait_for_review(created.review_id)
        await service.close()
        return settled

    settled = asyncio.run(scenario())
    assert settled.status is ReviewStatus.FAILED
    assert settled.error == "invalid_model_output"


def test_weekly_service_trims_extra_confidence_notes(tmp_path: Path) -> None:
    period = week_period(WEEK, timezone=TZ)
    notes = [f"note {index}" for index in range(6)]
    service, _ = _service(
        tmp_path,
        _weekly_input(period, (_task_source("task-1"),)),
        llm=FakeLLM(_payload(period, confidence_notes=notes)),
    )

    async def scenario() -> Any:
        created = await service.generate_weekly_review(
            WEEK, "weekly-extra-notes", timezone=TZ
        )
        settled = await service.wait_for_review(created.review_id)
        await service.close()
        return settled

    settled = asyncio.run(scenario())
    assert settled.status is ReviewStatus.COMPLETED
    assert isinstance(settled.summary, WeeklyReview)
    assert settled.summary.confidence_notes == tuple(notes[:5])
