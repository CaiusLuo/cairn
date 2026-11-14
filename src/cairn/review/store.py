"""SQLite authority for generated review reports and their request receipts.

The tables live in the same ``knowledge.sqlite3`` file as K1 Clips.  A missing
row is never confused with a failed model call: every creation records its
source manifest before any provider request starts, and only a
``pending -> generating -> completed`` transition can produce a report.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, cast

from cairn.knowledge.models import utc_now
from cairn.knowledge.store import KnowledgeStore
from cairn.review.models import (
    REVIEW_REQUEST_ID_MAX_LENGTH,
    DailyReview,
    DeliveryAttemptResult,
    ReviewCreateResult,
    ReviewDeliveryStatus,
    ReviewStatus,
    ReviewType,
    SourceCollectionStatus,
    SourceManifestEntry,
    SourceSnapshot,
    StoredReview,
    StoredReviewDelivery,
    new_review_id,
)

MAX_LIST_LIMIT = 100
DEFAULT_LIST_LIMIT = 20


class ReviewRequestConflict(RuntimeError):
    """A request_id was reused for a different review input."""


class ReviewPeriodCompleted(RuntimeError):
    """A completed report exists and regeneration was not explicit."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value: str | None, default: Any) -> Any:
    return default if not value else json.loads(value)


class ReviewStore:
    """One connection per operation; SQLite provides the write serialization."""

    def __init__(self, source: KnowledgeStore | Path) -> None:
        self.knowledge = (
            source if isinstance(source, KnowledgeStore) else KnowledgeStore(source)
        )
        self.path = self.knowledge.path
        if self.path.is_symlink():
            raise ValueError("Review store cannot be a symlink")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _review(row: sqlite3.Row) -> StoredReview:
        try:
            review_type = ReviewType(cast(str, row["review_type"]))
            status = ReviewStatus(cast(str, row["status"]))
            manifest = tuple(
                SourceManifestEntry.model_validate(item)
                for item in cast(
                    list[dict[str, Any]],
                    _loads(row["source_manifest_json"], []),
                )
            )
            collection = tuple(
                SourceCollectionStatus.model_validate(item)
                for item in cast(
                    list[dict[str, Any]],
                    _loads(row["collection_status_json"], []),
                )
            )
            source_snapshots = tuple(
                SourceSnapshot.model_validate(item)
                for item in cast(
                    list[dict[str, Any]],
                    _loads(row["source_snapshots_json"], []),
                )
            )
            summary_json = cast(str | None, row["summary_json"])
            summary = (
                None
                if summary_json is None
                else DailyReview.model_validate_json(summary_json)
            )
            return StoredReview(
                review_id=cast(str, row["review_id"]),
                review_type=review_type,
                date=date.fromisoformat(cast(str, row["review_date"])),
                period_start=cast(str, row["period_start"]),
                period_end=cast(str, row["period_end"]),
                timezone=cast(str, row["timezone"]),
                status=status,
                source_manifest=manifest,
                collection_status=collection,
                source_snapshots=source_snapshots,
                input_fingerprint=cast(str, row["input_fingerprint"]),
                summary=summary,
                summary_markdown=cast(str | None, row["summary_markdown"]),
                model_id=cast(str | None, row["model_id"]),
                generation_attempts=int(row["generation_attempts"]),
                created_at=cast(str, row["created_at"]),
                updated_at=cast(str, row["updated_at"]),
                completed_at=cast(str | None, row["completed_at"]),
                error=cast(str | None, row["error"]),
                version=int(row["version"]),
                supersedes_review_id=cast(str | None, row["supersedes_review_id"]),
            )
        except (ValueError, TypeError) as exc:
            raise ValueError("review store contains an invalid row") from exc

    @staticmethod
    def _delivery(row: sqlite3.Row) -> StoredReviewDelivery:
        try:
            return StoredReviewDelivery(
                delivery_id=cast(str, row["delivery_id"]),
                review_id=cast(str, row["review_id"]),
                review_version=int(row["review_version"]),
                channel=cast(str, row["channel"]),
                account_id=cast(str, row["account_id"]),
                recipient=cast(str, row["recipient"]),
                status=ReviewDeliveryStatus(cast(str, row["status"])),
                attempt_id=cast(str, row["attempt_id"]),
                attempts=int(row["attempts"]),
                context_token=cast(str, row["context_token"]),
                last_error=cast(str | None, row["last_error"]),
                created_at=cast(str, row["created_at"]),
                attempted_at=cast(str | None, row["attempted_at"]),
                completed_at=cast(str | None, row["completed_at"]),
                updated_at=cast(str, row["updated_at"]),
            )
        except (ValueError, TypeError) as exc:
            raise ValueError("review store contains an invalid delivery row") from exc

    @staticmethod
    def _request_fingerprint(
        *,
        review_type: ReviewType,
        day: date,
        timezone: str,
        regenerate: bool,
    ) -> str:
        # Request identity is the caller's intent, not the current source
        # fingerprint. A retry may collect newly added or edited sources and
        # still map back to the review accepted by the first request.
        payload = [
            review_type.value,
            day.isoformat(),
            timezone,
            bool(regenerate),
        ]
        return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()

    def create_or_get(
        self,
        *,
        request_id: str,
        review_type: ReviewType,
        day: date,
        period_start: str,
        period_end: str,
        timezone: str,
        input_fingerprint: str,
        source_manifest: tuple[SourceManifestEntry, ...],
        collection_status: tuple[SourceCollectionStatus, ...],
        source_snapshots: tuple[SourceSnapshot, ...] = (),
        regenerate: bool = False,
    ) -> ReviewCreateResult:
        """Atomically create a review or return the requested/idempotent one.

        Completed versions are immutable. A caller that wants a newer completed
        report after the sources changed must set ``regenerate=True``; that
        creates a new version and leaves the previous one intact.
        """
        request_key = request_id.strip()
        if not request_key:
            raise ValueError("request_id must not be blank")
        if len(request_key) > REVIEW_REQUEST_ID_MAX_LENGTH:
            raise ValueError("request_id is too long")
        request_fingerprint = self._request_fingerprint(
            review_type=review_type,
            day=day,
            timezone=timezone,
            regenerate=regenerate,
        )
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT fingerprint, review_id FROM review_requests "
                "WHERE request_id = ?",
                (request_key,),
            ).fetchone()
            if existing is not None:
                if existing["fingerprint"] != request_fingerprint:
                    raise ReviewRequestConflict(
                        "request_id was already used for a different review"
                    )
                row = connection.execute(
                    "SELECT * FROM reviews WHERE review_id = ?",
                    (existing["review_id"],),
                ).fetchone()
                if row is None:
                    raise ValueError("review request points at a missing review")
                return ReviewCreateResult(
                    review=self._review(row),
                    created=False,
                    reason="request_replay",
                )

            active = connection.execute(
                "SELECT * FROM reviews WHERE review_type = ? "
                "AND period_start = ? AND period_end = ? AND timezone = ? "
                "AND status IN ('pending', 'generating') "
                "ORDER BY created_at DESC LIMIT 1",
                (
                    review_type.value,
                    period_start,
                    period_end,
                    timezone,
                ),
            ).fetchone()
            if active is not None:
                connection.execute(
                    "INSERT INTO review_requests(request_id, fingerprint, review_id) "
                    "VALUES (?, ?, ?)",
                    (request_key, request_fingerprint, active["review_id"]),
                )
                return ReviewCreateResult(
                    review=self._review(active),
                    created=False,
                    reason="active_reused",
                )

            completed = connection.execute(
                "SELECT * FROM reviews WHERE review_type = ? "
                "AND period_start = ? AND period_end = ? AND timezone = ? "
                "AND status = 'completed' "
                "ORDER BY version DESC, created_at DESC LIMIT 1",
                (
                    review_type.value,
                    period_start,
                    period_end,
                    timezone,
                ),
            ).fetchone()
            if completed is not None and not regenerate:
                if completed["input_fingerprint"] != input_fingerprint:
                    raise ReviewPeriodCompleted(
                        "a completed review exists for this period; "
                        "regenerate explicitly to create a new version"
                    )
                connection.execute(
                    "INSERT INTO review_requests(request_id, fingerprint, review_id) "
                    "VALUES (?, ?, ?)",
                    (request_key, request_fingerprint, completed["review_id"]),
                )
                return ReviewCreateResult(
                    review=self._review(completed),
                    created=False,
                    reason="completed_reused",
                )

            latest = connection.execute(
                "SELECT review_id, version FROM reviews WHERE review_type = ? "
                "AND period_start = ? AND period_end = ? AND timezone = ? "
                "ORDER BY version DESC, created_at DESC LIMIT 1",
                (
                    review_type.value,
                    period_start,
                    period_end,
                    timezone,
                ),
            ).fetchone()
            version = 1 if latest is None else int(latest["version"]) + 1
            supersedes = None if latest is None else cast(str, latest["review_id"])
            review_id = new_review_id()
            connection.execute(
                """
                INSERT INTO reviews
                    (review_id, review_type, review_date, period_start, period_end,
                     timezone, status, source_manifest_json, collection_status_json,
                     source_snapshots_json, input_fingerprint, summary_json,
                     summary_markdown, model_id, generation_attempts, created_at,
                     updated_at, completed_at, error, version, supersedes_review_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 0, ?, ?,
                        NULL, NULL, ?, ?)
                """,
                (
                    review_id,
                    review_type.value,
                    day.isoformat(),
                    period_start,
                    period_end,
                    timezone,
                    ReviewStatus.PENDING.value,
                    _json([entry.model_dump(mode="json") for entry in source_manifest]),
                    _json(
                        [status.model_dump(mode="json") for status in collection_status]
                    ),
                    _json(
                        [source.model_dump(mode="json") for source in source_snapshots]
                    ),
                    input_fingerprint,
                    now,
                    now,
                    version,
                    supersedes,
                ),
            )
            connection.execute(
                "INSERT INTO review_requests(request_id, fingerprint, review_id) "
                "VALUES (?, ?, ?)",
                (request_key, request_fingerprint, review_id),
            )
            row = connection.execute(
                "SELECT * FROM reviews WHERE review_id = ?", (review_id,)
            ).fetchone()
            if row is None:
                raise ValueError("new review could not be read back")
            return ReviewCreateResult(
                review=self._review(row), created=True, reason="created"
            )

    def get(self, review_id: str) -> StoredReview | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM reviews WHERE review_id = ?", (review_id,)
            ).fetchone()
        return None if row is None else self._review(row)

    def list_reviews(
        self,
        *,
        review_type: ReviewType | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> tuple[StoredReview, ...]:
        if not 1 <= limit <= MAX_LIST_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIST_LIMIT}")
        if not 0 <= offset <= 1_000_000:
            raise ValueError("offset must be between 0 and 1000000")
        where = "" if review_type is None else " WHERE review_type = ?"
        params: list[object] = [] if review_type is None else [review_type.value]
        params.extend((limit, offset))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM reviews"
                + where
                + " ORDER BY created_at DESC, review_id DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return tuple(self._review(row) for row in rows)

    def mark_generating(self, review_id: str, model_id: str | None) -> StoredReview:
        """Claim a pending review; a second caller observes the same attempt."""
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM reviews WHERE review_id = ?", (review_id,)
            ).fetchone()
            if row is None:
                raise KeyError(review_id)
            current = self._review(row)
            if current.status is ReviewStatus.PENDING:
                connection.execute(
                    "UPDATE reviews SET status = ?, model_id = ?, "
                    "generation_attempts = generation_attempts + 1, updated_at = ? "
                    "WHERE review_id = ? AND status = 'pending'",
                    (
                        ReviewStatus.GENERATING.value,
                        model_id,
                        now,
                        review_id,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM reviews WHERE review_id = ?", (review_id,)
                ).fetchone()
                if row is None:
                    raise ValueError("review disappeared while generating")
                return self._review(row)
            return current

    def complete(
        self,
        review_id: str,
        *,
        summary: DailyReview,
        summary_markdown: str,
        model_id: str | None,
        request_delivery: bool = False,
    ) -> StoredReview:
        """Persist one completed report; a completed row is never overwritten.

        ``request_delivery`` inserts the pending delivery outbox row in the same
        transaction as completion. A crash after the commit therefore cannot
        lose the intent to send the report.
        """
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM reviews WHERE review_id = ?", (review_id,)
            ).fetchone()
            if row is None:
                raise KeyError(review_id)
            current = self._review(row)
            if current.status is not ReviewStatus.GENERATING:
                if request_delivery and current.status is ReviewStatus.COMPLETED:
                    self._ensure_delivery_unlocked(connection, review_id, now)
                return current
            connection.execute(
                "UPDATE reviews SET status = ?, summary_json = ?, "
                "summary_markdown = ?, model_id = COALESCE(?, model_id), "
                "completed_at = ?, updated_at = ?, error = NULL "
                "WHERE review_id = ? AND status = 'generating'",
                (
                    ReviewStatus.COMPLETED.value,
                    summary.model_dump_json(),
                    summary_markdown,
                    model_id,
                    now,
                    now,
                    review_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM reviews WHERE review_id = ?", (review_id,)
            ).fetchone()
            if row is None:
                raise ValueError("completed review could not be read back")
            if request_delivery:
                self._ensure_delivery_unlocked(connection, review_id, now)
            return self._review(row)

    def _ensure_delivery_unlocked(
        self, connection: sqlite3.Connection, review_id: str, now: str
    ) -> StoredReviewDelivery:
        review = connection.execute(
            "SELECT status, version FROM reviews WHERE review_id = ?", (review_id,)
        ).fetchone()
        if review is None:
            raise KeyError(review_id)
        if review["status"] != ReviewStatus.COMPLETED.value:
            raise ValueError("only a completed review can be delivered")
        existing = connection.execute(
            "SELECT * FROM review_deliveries WHERE review_id = ?", (review_id,)
        ).fetchone()
        if existing is not None:
            return self._delivery(existing)
        delivery_id = hashlib.sha256(
            f"review-delivery:{review_id}".encode()
        ).hexdigest()
        connection.execute(
            "INSERT INTO review_deliveries(delivery_id, review_id, "
            "review_version, channel, account_id, recipient, status, attempt_id, "
            "attempts, context_token, last_error, created_at, attempted_at, "
            "completed_at, updated_at) VALUES (?, ?, ?, 'wechat', '', '', ?, '', "
            "0, '', NULL, ?, NULL, NULL, ?)",
            (
                delivery_id,
                review_id,
                int(review["version"]),
                ReviewDeliveryStatus.PENDING.value,
                now,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM review_deliveries WHERE review_id = ?", (review_id,)
        ).fetchone()
        if row is None:
            raise ValueError("review delivery could not be read back")
        return self._delivery(row)

    def ensure_delivery(self, review_id: str) -> StoredReviewDelivery:
        """Create the pending delivery row if it is missing; never reset state."""
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._ensure_delivery_unlocked(connection, review_id, now)

    def get_delivery(self, review_id: str) -> StoredReviewDelivery | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM review_deliveries WHERE review_id = ?", (review_id,)
            ).fetchone()
        return None if row is None else self._delivery(row)

    def list_deliveries(
        self,
        *,
        statuses: frozenset[ReviewDeliveryStatus] | None = None,
    ) -> tuple[StoredReviewDelivery, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM review_deliveries ORDER BY created_at, review_id"
            ).fetchall()
        deliveries = tuple(self._delivery(row) for row in rows)
        if statuses is None:
            return deliveries
        return tuple(row for row in deliveries if row.status in statuses)

    def begin_delivery(
        self,
        review_id: str,
        *,
        account_id: str,
        sender_id: str,
        context_token: str | None = None,
    ) -> DeliveryAttemptResult:
        """Persist the attempt and bind its identity before any network send."""
        if not account_id or not sender_id:
            raise ValueError("delivery requires an exact account and recipient")
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM review_deliveries WHERE review_id = ?", (review_id,)
            ).fetchone()
            if row is None:
                raise KeyError(review_id)
            current = self._delivery(row)
            if current.status not in (
                ReviewDeliveryStatus.PENDING,
                ReviewDeliveryStatus.BLOCKED_OFFLINE,
            ):
                return DeliveryAttemptResult(
                    delivery=current,
                    allowed=False,
                    reason=current.status.value,
                )
            if current.account_id and (
                current.account_id != account_id or current.recipient != sender_id
            ):
                connection.execute(
                    "UPDATE review_deliveries SET status = ?, last_error = ?, "
                    "completed_at = ?, updated_at = ? WHERE review_id = ?",
                    (
                        ReviewDeliveryStatus.FAILED_PERMANENT.value,
                        "delivery identity changed",
                        now,
                        now,
                        review_id,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM review_deliveries WHERE review_id = ?",
                    (review_id,),
                ).fetchone()
                if row is None:
                    raise ValueError("review delivery disappeared")
                return DeliveryAttemptResult(
                    delivery=self._delivery(row),
                    allowed=False,
                    reason="identity_changed",
                )
            attempt_id = uuid.uuid4().hex
            connection.execute(
                "UPDATE review_deliveries SET status = ?, account_id = ?, "
                "recipient = ?, context_token = ?, attempt_id = ?, "
                "attempts = attempts + 1, last_error = NULL, attempted_at = ?, "
                "completed_at = NULL, updated_at = ? WHERE review_id = ?",
                (
                    ReviewDeliveryStatus.SENDING.value,
                    account_id,
                    sender_id,
                    context_token or current.context_token,
                    attempt_id,
                    now,
                    now,
                    review_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM review_deliveries WHERE review_id = ?", (review_id,)
            ).fetchone()
            if row is None:
                raise ValueError("review delivery could not be read back")
            return DeliveryAttemptResult(delivery=self._delivery(row), allowed=True)

    def mark_delivery_accepted(self, review_id: str) -> StoredReviewDelivery:
        """The upstream accepted the message; this does not imply it was read."""
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM review_deliveries WHERE review_id = ?", (review_id,)
            ).fetchone()
            if row is None:
                raise KeyError(review_id)
            current = self._delivery(row)
            if current.status is ReviewDeliveryStatus.ACCEPTED:
                return current
            if current.status is not ReviewDeliveryStatus.SENDING:
                raise ValueError("review delivery must be sending before accepted")
            connection.execute(
                "UPDATE review_deliveries SET status = ?, last_error = NULL, "
                "completed_at = ?, updated_at = ? WHERE review_id = ?",
                (ReviewDeliveryStatus.ACCEPTED.value, now, now, review_id),
            )
            row = connection.execute(
                "SELECT * FROM review_deliveries WHERE review_id = ?", (review_id,)
            ).fetchone()
            if row is None:
                raise ValueError("review delivery could not be read back")
            return self._delivery(row)

    def mark_delivery_unknown(
        self, review_id: str, error: str | None = None
    ) -> StoredReviewDelivery:
        """The send may have happened; it must never be retried automatically."""
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM review_deliveries WHERE review_id = ?", (review_id,)
            ).fetchone()
            if row is None:
                raise KeyError(review_id)
            current = self._delivery(row)
            if current.status is ReviewDeliveryStatus.ACCEPTED:
                raise ValueError("an accepted review delivery cannot become unknown")
            if current.status is not ReviewDeliveryStatus.SENDING:
                return current
            connection.execute(
                "UPDATE review_deliveries SET status = ?, last_error = ?, "
                "completed_at = ?, updated_at = ? WHERE review_id = ?",
                (
                    ReviewDeliveryStatus.DELIVERY_UNKNOWN.value,
                    (error or "delivery result unknown")[:500],
                    now,
                    now,
                    review_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM review_deliveries WHERE review_id = ?", (review_id,)
            ).fetchone()
            if row is None:
                raise ValueError("review delivery could not be read back")
            return self._delivery(row)

    def defer_delivery(self, review_id: str, error: str) -> StoredReviewDelivery:
        """A definite non-send: stay retryable until a fresh context arrives."""
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM review_deliveries WHERE review_id = ?", (review_id,)
            ).fetchone()
            if row is None:
                raise KeyError(review_id)
            current = self._delivery(row)
            if current.status is ReviewDeliveryStatus.BLOCKED_OFFLINE:
                return current
            if current.status is not ReviewDeliveryStatus.SENDING:
                raise ValueError("only a sending delivery can be deferred")
            connection.execute(
                "UPDATE review_deliveries SET status = ?, last_error = ?, "
                "completed_at = NULL, updated_at = ? WHERE review_id = ?",
                (
                    ReviewDeliveryStatus.BLOCKED_OFFLINE.value,
                    error[:500],
                    now,
                    review_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM review_deliveries WHERE review_id = ?", (review_id,)
            ).fetchone()
            if row is None:
                raise ValueError("review delivery could not be read back")
            return self._delivery(row)

    def requeue_delivery(
        self, review_id: str, *, rebind: bool = False
    ) -> StoredReviewDelivery:
        """Explicit operator retry after a failed or unknown delivery result."""
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM review_deliveries WHERE review_id = ?", (review_id,)
            ).fetchone()
            if row is None:
                raise KeyError(review_id)
            current = self._delivery(row)
            if current.status not in (
                ReviewDeliveryStatus.DELIVERY_UNKNOWN,
                ReviewDeliveryStatus.FAILED_PERMANENT,
                ReviewDeliveryStatus.BLOCKED_OFFLINE,
            ):
                raise ValueError(
                    "only a blocked, unknown or permanently failed delivery can retry"
                )
            connection.execute(
                "UPDATE review_deliveries SET status = ?, account_id = ?, "
                "recipient = ?, context_token = ?, attempt_id = '', "
                "last_error = NULL, completed_at = NULL, updated_at = ? "
                "WHERE review_id = ?",
                (
                    ReviewDeliveryStatus.PENDING.value,
                    "" if rebind else current.account_id,
                    "" if rebind else current.recipient,
                    "" if rebind else current.context_token,
                    now,
                    review_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM review_deliveries WHERE review_id = ?", (review_id,)
            ).fetchone()
            if row is None:
                raise ValueError("review delivery could not be read back")
            return self._delivery(row)

    def recover_sending_deliveries(self) -> int:
        """A sending row is in the unknown window; never resend it automatically."""
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT review_id FROM review_deliveries WHERE status = 'sending'"
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE review_deliveries SET status = ?, last_error = ?, "
                    "completed_at = ?, updated_at = ? WHERE review_id = ? "
                    "AND status = 'sending'",
                    (
                        ReviewDeliveryStatus.DELIVERY_UNKNOWN.value,
                        "delivery result unknown after restart",
                        now,
                        now,
                        cast(str, row["review_id"]),
                    ),
                )
        return len(rows)

    def fail(self, review_id: str, error: str) -> StoredReview | None:
        """Record an explicit failure only while the review is not completed."""
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM reviews WHERE review_id = ?", (review_id,)
            ).fetchone()
            if row is None:
                return None
            current = self._review(row)
            if current.status not in {
                ReviewStatus.PENDING,
                ReviewStatus.GENERATING,
            }:
                return current
            connection.execute(
                "UPDATE reviews SET status = ?, error = ?, updated_at = ? "
                "WHERE review_id = ? AND status IN ('pending', 'generating')",
                (
                    ReviewStatus.FAILED.value,
                    error[:200],
                    now,
                    review_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM reviews WHERE review_id = ?", (review_id,)
            ).fetchone()
            return None if row is None else self._review(row)

    def fail_incomplete(self, error: str = "interrupted_by_restart") -> int:
        """Make a crashed generation explicit after the process starts again."""
        now = utc_now()
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE reviews SET status = ?, error = ?, updated_at = ? "
                "WHERE status IN ('pending', 'generating')",
                (ReviewStatus.FAILED.value, error[:200], now),
            )
            return int(result.rowcount)


__all__ = [
    "DEFAULT_LIST_LIMIT",
    "MAX_LIST_LIMIT",
    "ReviewPeriodCompleted",
    "ReviewRequestConflict",
    "ReviewStore",
]
