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
    ReviewInput,
    ReviewType,
    SourceCollectionState,
    SourceCollectionStatus,
    SourceSnapshot,
    SourceTrust,
)
from cairn.shared.redaction import redact_text

MAX_INPUT_CHARS = 60_000
MAX_SOURCE_CHARS = 8_000
CLIP_CONTENT_CHARS = 6_000
MEMORY_ENTRY_CHARS = 4_000

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

    def collect_daily(self, day: date, timezone: str) -> ReviewInput:
        if not isinstance(day, date):
            raise ValueError("date must be a date")
        if not isinstance(timezone, str) or not timezone.strip():
            raise ValueError("timezone must not be blank")
        if len(timezone) > 64:
            raise ValueError("timezone is too long")
        try:
            zone = ZoneInfo(timezone.strip())
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("unknown timezone") from None

        start_local = datetime.combine(day, time.min, tzinfo=zone)
        end_local = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
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

        memory, memory_status = self._collect_memory(day, zone)
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
        self, day: date, zone: ZoneInfo
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
        try:
            body = self.memory.read_daily(day)
        except (OSError, ValueError) as exc:
            return [], _status(
                "personal_memory",
                SourceCollectionState.UNAVAILABLE,
                notes=(f"Daily journal unavailable ({type(exc).__name__}).",),
            )
        if not body.strip():
            return [], _status(
                "personal_memory",
                SourceCollectionState.EMPTY,
                notes=(
                    "No daily journal entry for this local date.",
                    "MEMORY.md has no per-entry timestamp and is not included.",
                ),
            )
        blocks = [
            block.strip()
            for block in re.split(r"(?m)(?=^## )", body)
            if block.strip().startswith("## ")
        ]
        snapshots: list[SourceSnapshot] = []
        skipped = 0
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
                day, time(hour=hour, minute=minute), tzinfo=zone
            )
            raw_hash = _sha256(block)
            source_line = self._journal_field(block, "source")
            kind_line = self._journal_field(block, "kind")
            explicit = source_line == "user_explicit"
            snapshot_body = redact_text(block, self.sensitive_values)
            snapshot_body, body_truncated = _truncate(snapshot_body, MEMORY_ENTRY_CHARS)
            payload = {
                "date": day.isoformat(),
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
                        f"memory-{day.isoformat()}-{hour:02d}{minute:02d}-"
                        f"{raw_hash[:12]}"
                    ),
                    content_hash=raw_hash,
                    occurred_at=occurred.isoformat(),
                    trust=(
                        SourceTrust.USER_RECORDED
                        if explicit
                        else SourceTrust.AGENT_INFERRED
                    ),
                    may_prove_completion=False,
                    verification=("user_recorded" if explicit else "agent_inferred"),
                    snapshot=snapshot,
                    truncated=body_truncated or truncated,
                )
            )
        notes = [
            "MEMORY.md has no per-entry timestamp and is not included.",
            "agent_inferred entries are Cairn's own journal, not proof of "
            "user completion.",
        ]
        if skipped:
            notes.append(
                f"{skipped} journal block(s) had no valid local HH:MM time and "
                "were not included."
            )
        state = (
            SourceCollectionState.OK
            if snapshots
            else SourceCollectionState.EMPTY
            if not blocks
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


__all__ = [
    "MAX_INPUT_CHARS",
    "MAX_SOURCE_CHARS",
    "ReviewSourceCollector",
]
