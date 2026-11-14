"""Review records are durable, versioned and survive K1 database upgrades."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pytest

from cairn.knowledge.models import content_hash
from cairn.knowledge.store import KnowledgeStore
from cairn.review.models import (
    DailyReview,
    ReviewCreateResult,
    ReviewDeliveryStatus,
    ReviewItem,
    ReviewStatus,
    ReviewType,
    SourceCollectionState,
    SourceCollectionStatus,
    SourceManifestEntry,
    SourceSnapshot,
    SourceTrust,
)
from cairn.review.store import (
    ReviewPeriodCompleted,
    ReviewRequestConflict,
    ReviewStore,
)

DAY = date(2026, 1, 15)
TZ = "Asia/Shanghai"
START = "2026-01-14T16:00:00+00:00"
END = "2026-01-15T16:00:00+00:00"
FINGERPRINT = "a" * 64


def _manifest(
    source_id: str = "run-1", digest: str = "b" * 64
) -> tuple[SourceManifestEntry, ...]:
    return (
        SourceManifestEntry(
            source_type="development_run",
            source_id=source_id,
            content_hash=digest,
            occurred_at="2026-01-15T02:00:00+00:00",
            trust=SourceTrust.SYSTEM_RECEIPT,
            may_prove_completion=True,
            verification="run_status=completed",
        ),
    )


def _status() -> tuple[SourceCollectionStatus, ...]:
    return (
        SourceCollectionStatus(
            source_type="development_run",
            status=SourceCollectionState.OK,
            discovered=1,
            included=1,
        ),
    )


def _create(
    store: ReviewStore,
    *,
    request_id: str = "request-1",
    fingerprint: str = FINGERPRINT,
    regenerate: bool = False,
    review_type: ReviewType = ReviewType.DAILY,
    day: date = DAY,
    start: str = START,
    end: str = END,
    timezone: str = TZ,
    source_snapshots: tuple[SourceSnapshot, ...] = (),
) -> ReviewCreateResult:
    return store.create_or_get(
        request_id=request_id,
        review_type=review_type,
        day=day,
        period_start=start,
        period_end=end,
        timezone=timezone,
        input_fingerprint=fingerprint,
        source_manifest=_manifest(),
        collection_status=_status(),
        source_snapshots=source_snapshots,
        regenerate=regenerate,
    )


def _summary(
    day: date = DAY, text: str = "Completed the release check."
) -> DailyReview:
    return DailyReview(
        date=day,
        overview="One verified execution completed.",
        completed_activities=(ReviewItem(text=text, source_refs=("run-1",)),),
        source_refs=("run-1",),
    )


def _complete(store: ReviewStore, review_id: str, summary: DailyReview) -> None:
    store.mark_generating(review_id, "test-model")
    store.complete(
        review_id,
        summary=summary,
        summary_markdown="# report\n",
        model_id="test-model",
    )


def test_k1_database_upgrades_without_losing_clips(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()
    path = root / "knowledge.sqlite3"
    original = "  原文保持\ncode\n"
    digest = content_hash(original)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE clips (
            clip_id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            source TEXT NOT NULL,
            tags_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            archived_at TEXT
        );
        CREATE TABLE clip_requests (
            request_id TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            clip_id TEXT NOT NULL REFERENCES clips(clip_id) ON DELETE CASCADE
        );
        """
    )
    connection.execute(
        "INSERT INTO clips VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
        (
            "clip-k1",
            original,
            "web",
            '["kept"]',
            digest,
            "2026-01-14T16:00:00+00:00",
            "2026-01-14T16:00:00+00:00",
        ),
    )
    connection.execute("PRAGMA user_version=1")
    connection.commit()
    connection.close()

    store = KnowledgeStore(root)
    assert store.get("clip-k1") is not None
    assert store.get("clip-k1").content == original  # type: ignore[union-attr]

    with sqlite3.connect(path) as check:
        version = check.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in check.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert version == 4
    assert {
        "clips",
        "clip_requests",
        "reviews",
        "review_requests",
        "review_deliveries",
    } <= tables

    reviews = ReviewStore(store)
    result = _create(reviews)
    assert result.review.status is ReviewStatus.PENDING


def test_v2_reviews_upgrade_without_losing_existing_reports(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()
    path = root / "knowledge.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE reviews (
            review_id TEXT PRIMARY KEY,
            review_type TEXT NOT NULL,
            review_date TEXT NOT NULL,
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL,
            timezone TEXT NOT NULL,
            status TEXT NOT NULL,
            source_manifest_json TEXT NOT NULL,
            collection_status_json TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL,
            summary_json TEXT,
            summary_markdown TEXT,
            model_id TEXT,
            generation_attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            error TEXT,
            version INTEGER NOT NULL DEFAULT 1,
            supersedes_review_id TEXT REFERENCES reviews(review_id)
        );
        """
    )
    connection.execute(
        "INSERT INTO reviews VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, "
        "NULL, 0, ?, ?, NULL, ?, 1, NULL)",
        (
            "review-v2",
            "daily",
            DAY.isoformat(),
            START,
            END,
            TZ,
            "failed",
            "[]",
            "[]",
            FINGERPRINT,
            "2026-01-15T00:00:00+00:00",
            "2026-01-15T00:01:00+00:00",
            "interrupted_by_restart",
        ),
    )
    connection.execute("PRAGMA user_version=2")
    connection.commit()
    connection.close()

    store = KnowledgeStore(root)
    reviews = ReviewStore(store)
    preserved = reviews.get("review-v2")
    assert preserved is not None
    assert preserved.status is ReviewStatus.FAILED
    assert preserved.source_snapshots == ()

    with sqlite3.connect(path) as check:
        version = check.execute("PRAGMA user_version").fetchone()[0]
        columns = {
            row[1] for row in check.execute("PRAGMA table_info(reviews)").fetchall()
        }
    assert version == 4
    assert "source_snapshots_json" in columns
    assert _create(reviews, request_id="after-upgrade").created is True


def test_request_replay_survives_source_changes_but_not_intent_changes(
    tmp_path: Path,
) -> None:
    store = ReviewStore(tmp_path / "knowledge")
    first = _create(store, request_id="same-request")
    assert first.created is True
    assert first.reason == "created"

    replay = _create(store, request_id="same-request")
    assert replay.created is False
    assert replay.reason == "request_replay"
    assert replay.review.review_id == first.review.review_id

    # A retry can observe newly added or edited Clips; that must still map to
    # the review accepted by the first request instead of conflicting.
    changed_sources = _create(store, request_id="same-request", fingerprint="c" * 64)
    assert changed_sources.created is False
    assert changed_sources.reason == "request_replay"
    assert changed_sources.review.review_id == first.review.review_id

    # The same request_id with a different intent is still a conflict.
    with pytest.raises(ReviewRequestConflict):
        _create(
            store,
            request_id="same-request",
            fingerprint="c" * 64,
            timezone="UTC",
        )
    with pytest.raises(ReviewRequestConflict):
        _create(
            store,
            request_id="same-request",
            fingerprint="c" * 64,
            regenerate=True,
        )

    other = _create(store, request_id="other-request", fingerprint="d" * 64)
    assert other.created is False
    assert other.reason == "active_reused"
    assert other.review.review_id == first.review.review_id
    assert len(store.list_reviews()) == 1


def test_source_snapshots_are_persisted_with_the_report(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "knowledge")
    snapshot = SourceSnapshot(
        source_type="knowledge_clip",
        source_id="clip-1",
        content_hash="b" * 64,
        occurred_at="2026-01-15T02:00:00+00:00",
        trust=SourceTrust.USER_PROVIDED,
        snapshot="the exact redacted text used for generation",
    )
    created = _create(
        store,
        request_id="snapshot-request",
        source_snapshots=(snapshot,),
    )
    reloaded = ReviewStore(store.knowledge).get(created.review.review_id)
    assert reloaded is not None
    assert reloaded.source_snapshots == (snapshot,)
    assert reloaded.source_snapshots[0].manifest_entry().source_id == "clip-1"


def test_concurrent_distinct_requests_reuse_one_active_review(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "knowledge")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda index: _create(store, request_id=f"request-{index}"),
                range(8),
            )
        )
    assert len({result.review.review_id for result in results}) == 1
    assert sum(result.created for result in results) == 1
    assert len(store.list_reviews()) == 1


def test_completed_reviews_are_immutable_and_regeneration_versions(
    tmp_path: Path,
) -> None:
    store = ReviewStore(tmp_path / "knowledge")
    created = _create(store, request_id="first").review
    _complete(store, created.review_id, _summary(text="first report"))

    replay = _create(store, request_id="replay")
    assert replay.reason == "completed_reused"
    assert replay.review.review_id == created.review_id
    assert replay.review.version == 1

    with pytest.raises(ReviewPeriodCompleted):
        _create(store, request_id="changed", fingerprint="e" * 64)

    regenerated = _create(
        store,
        request_id="regenerate",
        fingerprint="e" * 64,
        regenerate=True,
    )
    assert regenerated.created is True
    assert regenerated.review.version == 2
    assert regenerated.review.supersedes_review_id == created.review_id
    _complete(store, regenerated.review.review_id, _summary(text="second report"))

    old = store.get(created.review_id)
    new = store.get(regenerated.review.review_id)
    assert old is not None and old.summary is not None
    assert old.summary.completed_activities[0].text == "first report"
    assert new is not None and new.summary is not None
    assert new.summary.completed_activities[0].text == "second report"
    assert len(store.list_reviews()) == 2

    store.complete(
        created.review_id,
        summary=_summary(text="overwrite attempt"),
        summary_markdown="# overwritten\n",
        model_id="other-model",
    )
    store.fail(created.review_id, "overwrite attempt")
    unchanged = store.get(created.review_id)
    assert unchanged is not None and unchanged.summary is not None
    assert unchanged.status is ReviewStatus.COMPLETED
    assert unchanged.summary.completed_activities[0].text == "first report"
    assert unchanged.error is None


def test_attempts_recovery_and_failure_states(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "knowledge")
    pending = _create(store, request_id="pending").review
    generating = store.mark_generating(pending.review_id, "test-model")
    assert generating.status is ReviewStatus.GENERATING
    assert generating.generation_attempts == 1
    same_attempt = store.mark_generating(pending.review_id, "test-model")
    assert same_attempt.status is ReviewStatus.GENERATING
    assert same_attempt.generation_attempts == 1

    assert store.fail_incomplete() == 1
    interrupted = store.get(pending.review_id)
    assert interrupted is not None
    assert interrupted.status is ReviewStatus.FAILED
    assert interrupted.error == "interrupted_by_restart"
    assert interrupted.summary is None

    store.complete(
        pending.review_id,
        summary=_summary(),
        summary_markdown="# late\n",
        model_id="test-model",
    )
    still_failed = store.get(pending.review_id)
    assert still_failed is not None
    assert still_failed.status is ReviewStatus.FAILED
    assert still_failed.summary is None

    second = _create(store, request_id="second", fingerprint="f" * 64).review
    failed = store.fail(second.review_id, "model_timeout")
    assert failed is not None
    assert failed.status is ReviewStatus.FAILED
    assert failed.error == "model_timeout"
    assert store.fail_incomplete() == 0


def test_list_filters_type_and_pages(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "knowledge")
    first = _create(store, request_id="daily").review
    _complete(store, first.review_id, _summary())
    weekly = store.create_or_get(
        request_id="weekly",
        review_type=ReviewType.WEEKLY,
        day=DAY,
        period_start="2026-01-12T00:00:00+00:00",
        period_end="2026-01-18T16:00:00+00:00",
        timezone="Asia/Shanghai",
        input_fingerprint="9" * 64,
        source_manifest=_manifest(),
        collection_status=_status(),
    ).review

    assert [item.review_id for item in store.list_reviews()] == [
        weekly.review_id,
        first.review_id,
    ]
    assert [
        item.review_id for item in store.list_reviews(review_type=ReviewType.DAILY)
    ] == [first.review_id]
    assert len(store.list_reviews(limit=1)) == 1
    assert store.list_reviews(limit=1, offset=1)[0].review_id == first.review_id
    with pytest.raises(ValueError):
        store.list_reviews(limit=0)
    with pytest.raises(ValueError):
        store.list_reviews(offset=-1)


def _complete_with_delivery(store: ReviewStore, review_id: str) -> None:
    store.mark_generating(review_id, "test-model")
    store.complete(
        review_id,
        summary=_summary(),
        summary_markdown="# report\n",
        model_id="test-model",
        request_delivery=True,
    )


def test_completed_review_creates_one_pending_delivery(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "knowledge")
    created = _create(store, request_id="delivery-created").review
    _complete_with_delivery(store, created.review_id)

    delivery = store.get_delivery(created.review_id)
    assert delivery is not None
    assert delivery.status is ReviewDeliveryStatus.PENDING
    assert delivery.attempts == 0
    assert len(delivery.delivery_key) == 64

    again = store.ensure_delivery(created.review_id)
    assert again.delivery_key == delivery.delivery_key
    assert again.created_at == delivery.created_at


def test_delivery_identity_is_bound_before_send_and_switch_is_refused(
    tmp_path: Path,
) -> None:
    store = ReviewStore(tmp_path / "knowledge")
    created = _create(store, request_id="delivery-identity").review
    _complete_with_delivery(store, created.review_id)

    first = store.begin_delivery(
        created.review_id,
        account_id="bot-a",
        sender_id="owner",
        context_token="ctx-1",
    )
    assert first.allowed is True
    assert first.delivery.status is ReviewDeliveryStatus.STARTED
    assert first.delivery.attempts == 1

    # A second worker cannot claim the same started attempt.
    second = store.begin_delivery(
        created.review_id, account_id="bot-a", sender_id="owner"
    )
    assert second.allowed is False
    assert second.reason == "started"

    store.mark_delivery_unconfirmed(created.review_id, "send_unconfirmed")
    store.requeue_delivery(created.review_id)
    switched = store.begin_delivery(
        created.review_id, account_id="bot-b", sender_id="owner"
    )
    assert switched.allowed is False
    assert switched.reason == "identity_changed"
    assert switched.delivery.status is ReviewDeliveryStatus.MANUAL_REVIEW
    assert switched.delivery.last_error == "delivery identity changed"


def test_requeue_with_rebind_is_the_only_way_to_change_delivery_identity(
    tmp_path: Path,
) -> None:
    store = ReviewStore(tmp_path / "knowledge")
    created = _create(store, request_id="delivery-rebind").review
    _complete_with_delivery(store, created.review_id)
    store.begin_delivery(created.review_id, account_id="bot-a", sender_id="owner")
    store.mark_delivery_unconfirmed(created.review_id)
    store.requeue_delivery(created.review_id, rebind=True)

    rebound = store.begin_delivery(
        created.review_id, account_id="bot-b", sender_id="new-owner"
    )
    assert rebound.allowed is True
    assert rebound.delivery.attempts == 2
    assert rebound.delivery.status is ReviewDeliveryStatus.STARTED


def test_started_delivery_recovers_as_unconfirmed_without_resend(
    tmp_path: Path,
) -> None:
    store = ReviewStore(tmp_path / "knowledge")
    created = _create(store, request_id="delivery-recovery").review
    _complete_with_delivery(store, created.review_id)
    store.begin_delivery(created.review_id, account_id="bot-a", sender_id="owner")

    assert store.recover_started_deliveries() == 1
    recovered = store.get_delivery(created.review_id)
    assert recovered is not None
    assert recovered.status is ReviewDeliveryStatus.UNCONFIRMED
    assert recovered.last_error == "delivery result unknown after restart"
    assert (
        store.begin_delivery(
            created.review_id, account_id="bot-a", sender_id="owner"
        ).allowed
        is False
    )


def test_sent_delivery_cannot_be_requeued_or_unconfirmed(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "knowledge")
    created = _create(store, request_id="delivery-terminal").review
    _complete_with_delivery(store, created.review_id)
    store.begin_delivery(created.review_id, account_id="bot-a", sender_id="owner")
    sent = store.mark_delivery_sent(created.review_id)
    assert sent.status is ReviewDeliveryStatus.SENT

    with pytest.raises(ValueError):
        store.mark_delivery_unconfirmed(created.review_id)
    with pytest.raises(ValueError):
        store.requeue_delivery(created.review_id)
