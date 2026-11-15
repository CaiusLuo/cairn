"""Read-only collection of one day's review sources.

The collector turns durable Clips, Task/Run facts and the personal daily journal
into a bounded, deterministic ``ReviewInput``. It never mutates those stores and
never hides an unavailable or budget-omitted source: every discovered identity
remains in the manifest with its original content hash and collection state.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cairn.conversation.models import Run, RunStatus, workspace_id_for
from cairn.conversation.store import DomainStore
from cairn.development.models import Task, TaskStatus
from cairn.knowledge.models import Clip
from cairn.knowledge.store import KnowledgeStore
from cairn.memory.store import PersonalMemoryStore
from cairn.review.models import (
    DailyReview,
    ReviewInput,
    ReviewType,
    SourceCollectionState,
    SourceCollectionStatus,
    SourceManifestEntry,
    SourceSnapshot,
    SourceTrust,
    StoredReview,
)
from cairn.review.store import ReviewStore
from cairn.review.weekly import WeekPeriod
from cairn.shared.redaction import redact_text

MAX_INPUT_CHARS = 60_000
MAX_WEEKLY_INPUT_CHARS = 100_000
MAX_SOURCE_CHARS = 8_000
CLIP_CONTENT_CHARS = 6_000
MEMORY_ENTRY_CHARS = 4_000
WEEKLY_WINDOW_DAYS = 7

_MEMORY_HEADING = re.compile(r"^##\s+(\d{1,2}):(\d{2})\b")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    marker = f"\n...[truncated; original {len(text)} chars]"
    if len(marker) >= limit:
        return text[:limit], True
    return text[: limit - len(marker)] + marker, True


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _status(
    source_type: str,
    state: SourceCollectionState,
    *,
    discovered: int = 0,
    notes: tuple[str, ...] = (),
) -> SourceCollectionStatus:
    return SourceCollectionStatus(
        source_type=source_type,
        status=state,
        discovered=discovered,
        notes=notes,
    )


class ReviewSourceCollector:
    """Collect one local calendar day using an explicit half-open UTC window."""

    def __init__(
        self,
        *,
        knowledge: KnowledgeStore,
        workspace: Path | str,
        domain: DomainStore | None = None,
        memory: PersonalMemoryStore | None = None,
        memory_enabled: bool = True,
        sensitive_values: tuple[str, ...] = (),
        max_chars: int = MAX_INPUT_CHARS,
    ) -> None:
        self.knowledge = knowledge
        self.workspace = Path(workspace).expanduser().resolve()
        self.workspace_id = workspace_id_for(self.workspace)
        self.domain = domain
        self.memory = memory
        self.memory_enabled = memory_enabled
        self.sensitive_values = tuple(value for value in sensitive_values if value)
        if max_chars < 1:
            raise ValueError("max_chars must be positive")
        self.max_chars = max_chars

    def collect_daily(
        self,
        day: date,
        timezone: str,
        *,
        cutoff: time | None = None,
    ) -> ReviewInput:
        """Collect the half-open window ending at ``day``'s cutoff.

        The default window is the local calendar day. A host schedule may pass
        a daily cutoff such as 20:30; the window then runs from the previous
        day's cutoff to this day's cutoff, so late sources are carried into the
        next review without a gap or an overlap.
        """
        if not isinstance(day, date):
            raise ValueError("date must be a date")
        if not isinstance(timezone, str) or not timezone.strip():
            raise ValueError("timezone must not be blank")
        if len(timezone) > 64:
            raise ValueError("timezone is too long")
        if cutoff is not None and not isinstance(cutoff, time):
            raise ValueError("cutoff must be a time")
        try:
            zone = ZoneInfo(timezone.strip())
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("unknown timezone") from None

        if cutoff is None:
            start_local = datetime.combine(day, time.min, tzinfo=zone)
            end_local = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
        else:
            start_local = datetime.combine(day - timedelta(days=1), cutoff, tzinfo=zone)
            end_local = datetime.combine(day, cutoff, tzinfo=zone)
        start_utc = _utc_iso(start_local)
        end_utc = _utc_iso(end_local)

        sources: list[SourceSnapshot] = []
        statuses: list[SourceCollectionStatus] = []
        grouping: dict[str, list[SourceSnapshot]] = {
            "knowledge_clip": [],
            "development_task": [],
            "development_run": [],
            "personal_memory": [],
        }

        clips, clip_status = self._collect_clips(start_utc, end_utc)
        sources.extend(clips)
        grouping["knowledge_clip"].extend(clips)
        statuses.append(clip_status)
        statuses.append(
            _status(
                "knowledge_favorites",
                SourceCollectionState.UNAVAILABLE,
                notes=(
                    "K1 Clip has no favorite flag; every non-archived Clip "
                    "touched in the window was collected by updated_at.",
                    "Archived clips are excluded by default.",
                ),
            )
        )

        tasks, task_status = self._collect_tasks(start_utc, end_utc)
        sources.extend(tasks)
        grouping["development_task"].extend(tasks)
        statuses.append(task_status)
        runs, run_status = self._collect_runs(start_utc, end_utc)
        sources.extend(runs)
        grouping["development_run"].extend(runs)
        statuses.append(run_status)

        memory, memory_status = self._collect_memory(
            day,
            zone,
            cutoff=cutoff,
            start_local=start_local,
            end_local=end_local,
        )
        sources.extend(memory)
        grouping["personal_memory"].extend(memory)
        statuses.append(memory_status)

        sources = self._apply_budget(sources)
        final_by_type: dict[str, list[SourceSnapshot]] = {
            "knowledge_clip": [],
            "development_task": [],
            "development_run": [],
            "personal_memory": [],
        }
        for source in sources:
            final_by_type[source.source_type].append(source)

        final_statuses: list[SourceCollectionStatus] = []
        for status in statuses:
            if status.status is SourceCollectionState.UNAVAILABLE:
                final_statuses.append(status)
                continue
            group = final_by_type.get(status.source_type, [])
            # Memory blocks without a valid HH:MM are discovered omissions,
            # not sources that suddenly stopped existing.
            discovered = max(len(group), status.discovered)
            included = sum(1 for item in group if item.included)
            omitted = discovered - included
            state = (
                SourceCollectionState.PARTIAL
                if omitted > 0
                else SourceCollectionState.OK
                if discovered > 0
                else SourceCollectionState.EMPTY
            )
            final_statuses.append(
                SourceCollectionStatus(
                    source_type=status.source_type,
                    status=state,
                    discovered=discovered,
                    included=included,
                    omitted=omitted,
                    notes=status.notes,
                )
            )

        return ReviewInput.build(
            review_type=ReviewType.DAILY,
            day=day,
            timezone=timezone.strip(),
            period_start=start_local,
            period_end=end_local,
            sources=tuple(sources),
            collection_status=tuple(final_statuses),
            budget_chars=self.max_chars,
        )

    # -- source families --------------------------------------------------

    def _collect_clips(
        self, start_utc: str, end_utc: str
    ) -> tuple[list[SourceSnapshot], SourceCollectionStatus]:
        try:
            clips = self.knowledge.list_clips_touched_between(start_utc, end_utc)
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            return [], _status(
                "knowledge_clip",
                SourceCollectionState.UNAVAILABLE,
                notes=(f"KnowledgeStore unavailable ({type(exc).__name__}).",),
            )
        snapshots = [self._clip_source(clip) for clip in clips]
        return snapshots, _status(
            "knowledge_clip",
            SourceCollectionState.OK if snapshots else SourceCollectionState.EMPTY,
            discovered=len(snapshots),
            notes=("Archived clips are excluded by default.",),
        )

    def _collect_tasks(
        self, start_utc: str, end_utc: str
    ) -> tuple[list[SourceSnapshot], SourceCollectionStatus]:
        if self.domain is None:
            return [], _status(
                "development_task",
                SourceCollectionState.UNAVAILABLE,
                notes=("DomainStore was not configured.",),
            )
        try:
            tasks, runs = self.domain.review_activity_between(
                workspace_id=self.workspace_id, start=start_utc, end=end_utc
            )
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            return [], _status(
                "development_task",
                SourceCollectionState.UNAVAILABLE,
                notes=(f"Task/Run store unavailable ({type(exc).__name__}).",),
            )
        snapshots = [
            self._task_source(
                task, runs=tuple(run for run in runs if run.task_id == task.task_id)
            )
            for task in tasks
        ]
        return snapshots, _status(
            "development_task",
            SourceCollectionState.OK if snapshots else SourceCollectionState.EMPTY,
            discovered=len(snapshots),
        )

    def _collect_runs(
        self, start_utc: str, end_utc: str
    ) -> tuple[list[SourceSnapshot], SourceCollectionStatus]:
        if self.domain is None:
            return [], _status(
                "development_run",
                SourceCollectionState.UNAVAILABLE,
                notes=("DomainStore was not configured.",),
            )
        try:
            _, runs = self.domain.review_activity_between(
                workspace_id=self.workspace_id, start=start_utc, end=end_utc
            )
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            return [], _status(
                "development_run",
                SourceCollectionState.UNAVAILABLE,
                notes=(f"Task/Run store unavailable ({type(exc).__name__}).",),
            )
        snapshots = [self._run_source(run) for run in runs]
        return snapshots, _status(
            "development_run",
            SourceCollectionState.OK if snapshots else SourceCollectionState.EMPTY,
            discovered=len(snapshots),
        )

    def _collect_memory(
        self,
        day: date,
        zone: ZoneInfo,
        *,
        cutoff: time | None,
        start_local: datetime,
        end_local: datetime,
    ) -> tuple[list[SourceSnapshot], SourceCollectionStatus]:
        if self.memory is None:
            return [], _status(
                "personal_memory",
                SourceCollectionState.UNAVAILABLE,
                notes=("Personal memory store was not configured.",),
            )
        if not self.memory_enabled:
            return [], _status(
                "personal_memory",
                SourceCollectionState.UNAVAILABLE,
                notes=("Personal memory is disabled for this Server.",),
            )
        journal_days = (day,) if cutoff is None else (day - timedelta(days=1), day)
        bodies: list[tuple[date, str]] = []
        for journal_day in journal_days:
            try:
                body = self.memory.read_daily(journal_day)
            except (OSError, ValueError) as exc:
                return [], _status(
                    "personal_memory",
                    SourceCollectionState.UNAVAILABLE,
                    notes=(f"Daily journal unavailable ({type(exc).__name__}).",),
                )
            if body.strip():
                bodies.append((journal_day, body))
        if not bodies:
            return [], _status(
                "personal_memory",
                SourceCollectionState.EMPTY,
                notes=(
                    "No daily journal entry for this local window.",
                    "MEMORY.md has no per-entry timestamp and is not included.",
                ),
            )
        snapshots: list[SourceSnapshot] = []
        skipped = 0
        total_blocks = 0
        for journal_day, body in bodies:
            blocks = [
                block.strip()
                for block in re.split(r"(?m)(?=^## )", body)
                if block.strip().startswith("## ")
            ]
            total_blocks += len(blocks)
            for block in blocks:
                heading = block.splitlines()[0]
                match = _MEMORY_HEADING.match(heading)
                if match is None:
                    skipped += 1
                    continue
                hour, minute = int(match.group(1)), int(match.group(2))
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    skipped += 1
                    continue
                occurred = datetime.combine(
                    journal_day, time(hour=hour, minute=minute), tzinfo=zone
                )
                if cutoff is not None and not (start_local <= occurred < end_local):
                    continue
                raw_hash = _sha256(block)
                source_line = self._journal_field(block, "source")
                kind_line = self._journal_field(block, "kind")
                explicit = source_line == "user_explicit"
                snapshot_body = redact_text(block, self.sensitive_values)
                snapshot_body, body_truncated = _truncate(
                    snapshot_body, MEMORY_ENTRY_CHARS
                )
                payload = {
                    "date": journal_day.isoformat(),
                    "local_time": occurred.isoformat(),
                    "kind": kind_line,
                    "source": source_line or "agent_inferred",
                    "journal_entry": snapshot_body,
                }
                snapshot, truncated = _truncate(_json(payload), MAX_SOURCE_CHARS)
                snapshots.append(
                    SourceSnapshot(
                        source_type="personal_memory",
                        source_id=(
                            f"memory-{journal_day.isoformat()}-"
                            f"{hour:02d}{minute:02d}-{raw_hash[:12]}"
                        ),
                        content_hash=raw_hash,
                        occurred_at=occurred.isoformat(),
                        trust=(
                            SourceTrust.USER_RECORDED
                            if explicit
                            else SourceTrust.AGENT_INFERRED
                        ),
                        may_prove_completion=False,
                        verification=(
                            "user_recorded" if explicit else "agent_inferred"
                        ),
                        snapshot=snapshot,
                        truncated=body_truncated or truncated,
                    )
                )
        notes = [
            "MEMORY.md has no per-entry timestamp and is not included.",
            "agent_inferred entries are Cairn's own journal, not proof of "
            "user completion.",
        ]
        if cutoff is not None:
            notes.append(
                "Journal entries at or after the cutoff are carried into the "
                "next review."
            )
        if skipped:
            notes.append(
                f"{skipped} journal block(s) had no valid local HH:MM time and "
                "were not included."
            )
        state = (
            SourceCollectionState.OK
            if snapshots
            else SourceCollectionState.EMPTY
            if not total_blocks
            else SourceCollectionState.PARTIAL
        )
        return snapshots, SourceCollectionStatus(
            source_type="personal_memory",
            status=state,
            discovered=len(snapshots) + skipped,
            notes=tuple(notes),
        )

    @staticmethod
    def _journal_field(block: str, name: str) -> str:
        prefix = f"- {name}:"
        for line in block.splitlines():
            stripped = line.strip()
            if stripped.startswith(prefix):
                return stripped[len(prefix) :].strip()
        return ""

    # -- snapshots --------------------------------------------------------

    def _clip_source(self, clip: Clip) -> SourceSnapshot:
        content, content_truncated = _truncate(
            redact_text(clip.content, self.sensitive_values), CLIP_CONTENT_CHARS
        )
        payload = {
            "clip_id": clip.clip_id,
            "source": clip.source,
            "tags": list(clip.tags),
            "created_at": clip.created_at,
            "updated_at": clip.updated_at,
            "archived_at": clip.archived_at,
            "content": content,
        }
        snapshot, snapshot_truncated = _truncate(_json(payload), MAX_SOURCE_CHARS)
        return SourceSnapshot(
            source_type="knowledge_clip",
            source_id=clip.clip_id,
            content_hash=clip.content_hash,
            occurred_at=clip.updated_at,
            trust=SourceTrust.USER_PROVIDED,
            may_prove_completion=False,
            verification="user_provided_clip",
            snapshot=snapshot,
            truncated=content_truncated or snapshot_truncated,
        )

    def _task_source(self, task: Task, *, runs: tuple[Run, ...]) -> SourceSnapshot:
        metadata = task.metadata
        execution_status = metadata.get("execution_status")
        execution_status_text = (
            execution_status if isinstance(execution_status, str) else ""
        )
        accepted = task.status is TaskStatus.DONE
        execution_text = execution_status_text or "missing"
        verification = (
            f"business_status={task.status.value}; execution_status={execution_text}"
        )
        if execution_status_text == "completed" and not accepted:
            verification += "; execution_only"
        payload = {
            "task_id": task.task_id,
            "title": task.title,
            "goal": task.goal,
            "business_status": task.status.value,
            "accepted_by_operator": accepted,
            "execution_status": execution_status_text or None,
            "execution_diagnostic": metadata.get("execution_diagnostic"),
            "execution_result": metadata.get("execution_result"),
            "issue_url": task.issue_url,
            "worktree": task.worktree,
            "branch": task.branch,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "run_ids": [run.run_id for run in runs],
        }
        full = _json(payload)
        raw_hash = _sha256(full)
        snapshot, truncated = _truncate(
            redact_text(full, self.sensitive_values), MAX_SOURCE_CHARS
        )
        return SourceSnapshot(
            source_type="development_task",
            source_id=task.task_id,
            content_hash=raw_hash,
            occurred_at=task.updated_at,
            trust=SourceTrust.SYSTEM_RECEIPT,
            # A completed Run only proves execution. Operator acceptance is
            # represented by the Task's durable business status DONE.
            may_prove_completion=accepted,
            verification=verification,
            snapshot=snapshot,
            truncated=truncated,
        )

    def _run_source(self, run: Run) -> SourceSnapshot:
        payload = {
            "run_id": run.run_id,
            "task_id": run.task_id,
            "status": run.status.value,
            "source": None if run.source is None else run.source.value,
            "mode": run.mode,
            "prompt": run.prompt,
            "result": run.result,
            "error": run.error,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
            "ended_at": run.ended_at,
            "trace_ref": run.trace_ref,
        }
        full = _json(payload)
        raw_hash = _sha256(full)
        snapshot, truncated = _truncate(
            redact_text(full, self.sensitive_values), MAX_SOURCE_CHARS
        )
        return SourceSnapshot(
            source_type="development_run",
            source_id=run.run_id,
            content_hash=raw_hash,
            occurred_at=run.ended_at or run.updated_at,
            trust=SourceTrust.SYSTEM_RECEIPT,
            # Run completion is execution evidence only; it is not operator
            # acceptance that the intended goal was delivered.
            may_prove_completion=False,
            verification=(
                f"run_status={run.status.value}"
                + ("; execution_only" if run.status is RunStatus.COMPLETED else "")
            ),
            snapshot=snapshot,
            truncated=truncated,
        )

    def _apply_budget(self, sources: list[SourceSnapshot]) -> list[SourceSnapshot]:
        priority = {
            "development_task": 0,
            "development_run": 0,
            "knowledge_clip": 1,
            "personal_memory": 2,
        }
        ordered = sorted(sources, key=lambda item: item.occurred_at, reverse=True)
        ordered.sort(key=lambda item: priority.get(item.source_type, 9))
        used = 0
        result: list[SourceSnapshot] = []
        for source in ordered:
            cost = source.prompt_chars()
            if used + cost <= self.max_chars:
                result.append(source)
                used += cost
            else:
                result.append(
                    source.model_copy(
                        update={
                            "included": False,
                            "omitted_reason": "input_budget_exhausted",
                            "snapshot": "",
                        }
                    )
                )
        return result


class WeeklySourceCollector(ReviewSourceCollector):
    """Collect seven daily windows without double-counting shared sources.

    Completed daily reports are the primary summaries. Their persisted original
    snapshots (and manifest-only identities for older reports) are included once
    each so a weekly claim can cite the real Task/Clip/Run identity. When a
    daily report is missing, the same unmodified daily collector gathers that
    window's raw records and the gap is recorded explicitly.
    """

    def __init__(
        self,
        *,
        reviews: ReviewStore,
        knowledge: KnowledgeStore,
        workspace: Path | str,
        domain: DomainStore | None = None,
        memory: PersonalMemoryStore | None = None,
        memory_enabled: bool = True,
        sensitive_values: tuple[str, ...] = (),
        cutoff: time = time(20, 30),
        max_chars: int = MAX_WEEKLY_INPUT_CHARS,
    ) -> None:
        super().__init__(
            knowledge=knowledge,
            workspace=workspace,
            domain=domain,
            memory=memory,
            memory_enabled=memory_enabled,
            sensitive_values=sensitive_values,
            max_chars=max_chars,
        )
        self.reviews = reviews
        self.cutoff = cutoff

    def collect_weekly(self, period: WeekPeriod, timezone: str) -> ReviewInput:
        if not isinstance(timezone, str) or not timezone.strip():
            raise ValueError("timezone must not be blank")
        try:
            ZoneInfo(timezone.strip())
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("unknown timezone") from None
        if period.period_start.tzinfo is None or period.period_end.tzinfo is None:
            raise ValueError("weekly period boundaries must be timezone-aware")
        # Compare local dates, not absolute duration: a DST transition week is
        # still seven local days even though it spans 167 or 169 hours.
        start, end = period.period_start, period.period_end
        same_cutoff = start.timetz().replace(tzinfo=None) == end.timetz().replace(
            tzinfo=None
        )
        seven_days = end.date() == start.date() + timedelta(days=WEEKLY_WINDOW_DAYS)
        if not seven_days or not same_cutoff:
            raise ValueError("a weekly period must cover exactly seven days")

        daily_sources: list[SourceSnapshot] = []
        versions: dict[str, list[SourceSnapshot]] = {}
        missing_days: list[date] = []

        def observe(source: SourceSnapshot) -> None:
            versions.setdefault(source.source_id, []).append(source)

        for offset in range(WEEKLY_WINDOW_DAYS):
            start = period.period_start + timedelta(days=offset)
            end = start + timedelta(days=1)
            review = self.reviews.latest_completed(
                review_type=ReviewType.DAILY,
                period_start=start.isoformat(),
                period_end=end.isoformat(),
                timezone=timezone,
            )
            if review is None or review.summary is None:
                missing_days.append(end.date())
                daily_input = self.collect_daily(
                    end.date(), timezone, cutoff=self.cutoff
                )
                for source in daily_input.sources:
                    observe(source)
                continue
            daily_sources.append(self._daily_review_source(review, end))
            snapshot_ids = {source.source_id for source in review.source_snapshots}
            for source in review.source_snapshots:
                observe(source)
            for entry in review.source_manifest:
                if entry.source_id in snapshot_ids:
                    continue
                observe(self._identity_source(entry))

        originals: dict[str, SourceSnapshot] = {}
        duplicate_count = 0
        versioned_count = 0
        for source_id, occurrences in versions.items():
            merged, duplicates = self._merge_versions(occurrences)
            originals[source_id] = merged
            duplicate_count += duplicates
            if len({item.content_hash for item in occurrences}) > 1:
                versioned_count += 1

        sources = daily_sources + list(originals.values())
        discovered = {
            source_type: sum(1 for item in sources if item.source_type == source_type)
            for source_type in (
                "daily_review",
                "development_task",
                "development_run",
                "knowledge_clip",
                "personal_memory",
            )
        }
        discovered["daily_review"] = WEEKLY_WINDOW_DAYS
        sources = self._apply_weekly_budget(sources)

        notes: dict[str, list[str]] = {source_type: [] for source_type in discovered}
        if missing_days:
            notes["daily_review"].append(
                "缺少日报的日期: "
                + ", ".join(day.isoformat() for day in missing_days)
                + "; 这些日期使用原始记录补齐。"
            )
        else:
            notes["daily_review"].append("七个日报周期均有已完成日报。")
        if duplicate_count:
            notes["daily_review"].append(
                f"已去重 {duplicate_count} 次内容相同的重复来源记录。"
            )
        if versioned_count:
            notes["daily_review"].append(
                f"{versioned_count} 个来源在本周内发生过内容或状态变化, "
                "结果按最新纳入版本判断, 历史版本保留在快照中。"
            )
        notes["daily_review"].append(
            "日报是模型生成的二级总结, 不能扩大原始事实的证明范围。"
        )

        statuses: list[SourceCollectionStatus] = []
        for source_type, total in discovered.items():
            group = [item for item in sources if item.source_type == source_type]
            included = sum(1 for item in group if item.included)
            omitted = total - included
            state = (
                SourceCollectionState.PARTIAL
                if omitted > 0
                else SourceCollectionState.OK
                if included > 0
                else SourceCollectionState.EMPTY
            )
            statuses.append(
                SourceCollectionStatus(
                    source_type=source_type,
                    status=state,
                    discovered=total,
                    included=included,
                    omitted=omitted,
                    notes=tuple(notes[source_type]),
                )
            )

        return ReviewInput.build(
            review_type=ReviewType.WEEKLY,
            day=period.day,
            timezone=timezone.strip(),
            period_start=period.period_start,
            period_end=period.period_end,
            sources=tuple(sources),
            collection_status=tuple(statuses),
            budget_chars=self.max_chars,
        )

    def _daily_review_source(
        self, review: StoredReview, window_end: datetime
    ) -> SourceSnapshot:
        summary = review.summary
        if not isinstance(summary, DailyReview):
            raise ValueError("a daily review source must be a completed daily report")
        payload = {
            "review_id": review.review_id,
            "date": review.date.isoformat(),
            "version": review.version,
            "period_start": review.period_start,
            "period_end": review.period_end,
            "overview": summary.overview,
            "completed_activities": [
                item.model_dump(mode="json") for item in summary.completed_activities
            ],
            "knowledge_insights": [
                item.model_dump(mode="json") for item in summary.knowledge_insights
            ],
            "problems": [item.model_dump(mode="json") for item in summary.problems],
            "recommendations": [
                item.model_dump(mode="json") for item in summary.recommendations
            ],
            "confidence_notes": list(summary.confidence_notes),
            "source_manifest": [
                entry.model_dump(mode="json") for entry in review.source_manifest
            ],
        }
        full = _json(payload)
        raw_hash = _sha256(full)
        snapshot, truncated = _truncate(
            redact_text(full, self.sensitive_values), MAX_SOURCE_CHARS
        )
        return SourceSnapshot(
            source_type="daily_review",
            source_id=review.review_id,
            content_hash=raw_hash,
            occurred_at=review.completed_at or window_end.isoformat(),
            trust=SourceTrust.AGENT_INFERRED,
            may_prove_completion=False,
            verification="daily_review_summary",
            snapshot=snapshot,
            truncated=truncated,
        )

    @staticmethod
    def _identity_source(entry: SourceManifestEntry) -> SourceSnapshot:
        """Keep an older report's identity, but never let it prove completion."""
        verification = entry.verification
        if "identity_only" not in verification:
            suffix = "; identity_only" if verification else "identity_only"
            verification = verification[: 80 - len(suffix)] + suffix
        payload = {
            "source_id": entry.source_id,
            "source_type": entry.source_type,
            "occurred_at": entry.occurred_at,
            "content_hash": entry.content_hash,
            "trust": entry.trust.value,
            "may_prove_completion": False,
            "verification": verification,
            "included": entry.included,
            "omitted_reason": entry.omitted_reason,
            "truncated": entry.truncated,
            "evidence": "identity_only",
        }
        return SourceSnapshot(
            source_type=entry.source_type,
            source_id=entry.source_id,
            content_hash=entry.content_hash,
            occurred_at=entry.occurred_at,
            trust=entry.trust,
            may_prove_completion=False,
            verification=verification,
            snapshot=_json(payload),
            truncated=entry.truncated,
            included=entry.included,
            omitted_reason=entry.omitted_reason,
        )

    @staticmethod
    def _merge_versions(
        occurrences: list[SourceSnapshot],
    ) -> tuple[SourceSnapshot, int]:
        """One identity at its latest included state, with every version kept.

        Repeating the same content hash is a duplicate, not a new version. A
        later change replaces the effective state; an older DONE can never be
        used once a newer included version says otherwise. A version omitted
        from its daily report remains audit history and is not evidence.
        """
        unique: list[SourceSnapshot] = []
        index_by_hash: dict[str, int] = {}
        duplicates = 0
        latest_included: SourceSnapshot | None = None
        for occurrence in occurrences:
            if occurrence.included:
                latest_included = occurrence
            index = index_by_hash.get(occurrence.content_hash)
            if index is not None:
                duplicates += 1
                if occurrence.included and not unique[index].included:
                    unique[index] = occurrence
                continue
            index_by_hash[occurrence.content_hash] = len(unique)
            unique.append(occurrence)
        latest = latest_included or occurrences[-1]
        history = [
            {
                "content_hash": item.content_hash,
                "occurred_at": item.occurred_at,
                "trust": item.trust.value,
                "may_prove_completion": item.may_prove_completion,
                "verification": item.verification,
                "included": item.included,
                "omitted_reason": item.omitted_reason,
                "truncated": item.truncated,
            }
            for item in unique
        ]
        trail = [
            {
                "content_hash": item.content_hash,
                "occurred_at": item.occurred_at,
                "included": item.included,
            }
            for item in occurrences
        ]
        try:
            decoded = json.loads(latest.snapshot) if latest.snapshot else None
        except (json.JSONDecodeError, TypeError):
            decoded = None
        if isinstance(decoded, dict):
            decoded["observed_versions"] = history
            decoded["evidence_trail"] = trail
            merged = _json(decoded)
        else:
            merged = _json(
                {
                    "latest_snapshot": latest.snapshot,
                    "observed_versions": history,
                    "evidence_trail": trail,
                }
            )
        snapshot, truncated = _truncate(merged, MAX_SOURCE_CHARS)
        return (
            latest.model_copy(
                update={
                    "snapshot": snapshot,
                    "truncated": latest.truncated or truncated,
                    "may_prove_completion": (
                        latest.may_prove_completion if latest.included else False
                    ),
                    "included": latest.included,
                    "omitted_reason": (
                        None
                        if latest.included
                        else latest.omitted_reason or "omitted_from_daily_review"
                    ),
                }
            ),
            duplicates,
        )

    def _apply_weekly_budget(
        self, sources: list[SourceSnapshot]
    ) -> list[SourceSnapshot]:
        priority = {
            "daily_review": 0,
            "development_task": 1,
            "development_run": 2,
            "knowledge_clip": 3,
            "personal_memory": 4,
        }
        ordered = sorted(sources, key=lambda item: item.occurred_at, reverse=True)
        ordered.sort(key=lambda item: priority.get(item.source_type, 9))
        used = 0
        result: list[SourceSnapshot] = []
        for source in ordered:
            if not source.included:
                # A source omitted from its daily report stays omitted; the
                # weekly budget must not silently promote it into evidence.
                result.append(source.model_copy(update={"snapshot": ""}))
                continue
            cost = source.prompt_chars()
            if used + cost <= self.max_chars:
                result.append(source)
                used += cost
            else:
                result.append(
                    source.model_copy(
                        update={
                            "included": False,
                            "omitted_reason": "input_budget_exhausted",
                            "snapshot": "",
                        }
                    )
                )
        return result


__all__ = [
    "MAX_INPUT_CHARS",
    "MAX_SOURCE_CHARS",
    "MAX_WEEKLY_INPUT_CHARS",
    "ReviewSourceCollector",
    "WeeklySourceCollector",
]
