"""Daily review collection preserves identities, time windows and omissions."""

from __future__ import annotations

import json
from datetime import UTC, date, time
from pathlib import Path

import pytest

from cairn.conversation.models import ConversationSource, RunStatus
from cairn.conversation.store import DomainStore
from cairn.development.models import TaskStatus
from cairn.knowledge.document_store import DocumentStore
from cairn.knowledge.documents import (
    DOCUMENT_READY,
    STEP_SKIPPED,
    chunk_text,
)
from cairn.knowledge.retrieval import DocumentSearcher
from cairn.knowledge.store import KnowledgeStore
from cairn.memory.store import PersonalMemoryStore
from cairn.review.collector import ReviewSourceCollector
from cairn.review.models import SourceCollectionState, SourceTrust
from tests.support.pg import database_for

DAY = date(2026, 1, 15)
TZ = "Asia/Shanghai"
START = "2026-01-14T16:00:00+00:00"
END = "2026-01-15T16:00:00+00:00"


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def _clip_at(
    store: KnowledgeStore,
    content: str,
    updated_at: str,
    *,
    request_id: str,
    archived: bool = False,
) -> str:
    clip, _ = store.create(content, source="web", tags=["test"], request_id=request_id)
    if archived:
        store.set_archived(clip.clip_id, archived=True)
    with store.db.connect("knowledge") as connection:
        connection.execute(
            "UPDATE clips SET created_at = %s, updated_at = %s WHERE clip_id = %s",
            (updated_at, updated_at, clip.clip_id),
        )
    return clip.clip_id


def _domain(tmp_path: Path, workspace: Path) -> tuple[DomainStore, str]:
    domain = DomainStore(database_for(tmp_path))
    conversation = domain.create_conversation(
        workspace=workspace,
        source=ConversationSource.CLI,
        initial_prompt="review sources",
        request_id="review-conversation",
    )
    return domain, conversation.conversation_id


def _ready_document(
    documents: DocumentStore,
    title: str,
    text: str,
    *,
    created_at: str,
) -> str:
    document, _ = documents.create_upload(
        title, "text/markdown", f"# {title}\n\n{text}\n".encode()
    )
    documents.save_extraction(
        document.document_id,
        text,
        "text",
        "1",
        chunk_text(text, chunk_chars=400, overlap=0),
    )
    documents.finish(
        document.document_id,
        status=DOCUMENT_READY,
        error=None,
        embedding_status=STEP_SKIPPED,
        graph_status=STEP_SKIPPED,
    )
    with documents.db.connect("knowledge") as connection:
        connection.execute(
            "UPDATE documents SET created_at = %s, updated_at = %s "
            "WHERE document_id = %s",
            (created_at, created_at, document.document_id),
        )
    return document.document_id


def _collector(
    knowledge: KnowledgeStore,
    workspace: Path,
    *,
    domain: DomainStore | None = None,
    documents: DocumentStore | None = None,
    searcher: DocumentSearcher | None = None,
    memory: PersonalMemoryStore | None = None,
    memory_enabled: bool = True,
    max_chars: int = 60_000,
) -> ReviewSourceCollector:
    return ReviewSourceCollector(
        knowledge=knowledge,
        workspace=workspace,
        domain=domain,
        documents=documents,
        searcher=searcher,
        memory=memory,
        memory_enabled=memory_enabled,
        max_chars=max_chars,
    )


def test_utc_window_is_half_open_and_archived_clips_are_excluded(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    store = KnowledgeStore(database_for(tmp_path))
    before = _clip_at(
        store,
        "before",
        "2026-01-14T15:59:59+00:00",
        request_id="before",
    )
    at_start = _clip_at(
        store,
        "at start",
        START,
        request_id="at-start",
    )
    archived = _clip_at(
        store,
        "archived at start",
        START,
        request_id="archived-at-start",
        archived=True,
    )
    before_end = _clip_at(
        store,
        "before end",
        "2026-01-15T15:59:59+00:00",
        request_id="before-end",
    )
    after = _clip_at(
        store,
        "after",
        END,
        request_id="after",
    )

    review_input = _collector(store, workspace).collect_daily(DAY, TZ)
    ids = {source.source_id for source in review_input.sources}
    assert ids == {at_start, before_end}
    assert before not in ids and after not in ids
    assert archived not in ids
    assert review_input.period_start.astimezone(UTC).isoformat() == START
    assert review_input.period_end.astimezone(UTC).isoformat() == END

    clip_status = next(
        status
        for status in review_input.collection_status
        if status.source_type == "knowledge_clip"
    )
    assert clip_status.status is SourceCollectionState.OK
    assert clip_status.discovered == 2
    assert clip_status.included == 2

    stored = store.get(at_start)
    assert stored is not None
    assert stored.archived_at is None
    assert stored.content == "at start"
    assert stored.content_hash == next(
        source.content_hash
        for source in review_input.sources
        if source.source_id == at_start
    )

    archived_clip = store.get(archived)
    assert archived_clip is not None
    assert archived_clip.archived_at is not None
    assert archived_clip.content == "archived at start"


def test_archived_clip_is_restored_inside_the_window(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    store = KnowledgeStore(database_for(tmp_path))
    clip_id = _clip_at(
        store,
        "archive and restore",
        "2026-01-15T02:00:00+00:00",
        request_id="archive-restore",
        archived=True,
    )
    collector = _collector(store, workspace)
    assert clip_id not in {
        source.source_id for source in collector.collect_daily(DAY, TZ).sources
    }

    store.set_archived(clip_id, archived=False)
    with store.db.connect("knowledge") as connection:
        connection.execute(
            "UPDATE clips SET updated_at = %s WHERE clip_id = %s",
            ("2026-01-15T02:00:00+00:00", clip_id),
        )
    restored = collector.collect_daily(DAY, TZ)
    assert clip_id in {source.source_id for source in restored.sources}
    assert store.get(clip_id).archived_at is None  # type: ignore[union-attr]


def test_failed_and_completed_task_runs_are_not_confused(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    store = KnowledgeStore(database_for(tmp_path))
    domain, conversation_id = _domain(tmp_path, workspace)

    domain.create_task(
        conversation_id=conversation_id,
        goal="completed execution",
        task_id="task-completed",
        created_at="2026-01-14T17:00:00+00:00",
        metadata={
            "execution_status": "completed",
            "execution_result": "published release",
        },
    )
    domain.create_task(
        conversation_id=conversation_id,
        goal="failed execution",
        task_id="task-failed",
        created_at="2026-01-14T18:00:00+00:00",
        metadata={
            "execution_status": "failed",
            "execution_diagnostic": "tests failed",
        },
    )
    domain.create_task(
        conversation_id=conversation_id,
        goal="business done only",
        task_id="task-done-only",
        status=TaskStatus.DONE,
        created_at="2026-01-14T19:00:00+00:00",
    )
    domain.create_task(
        conversation_id=conversation_id,
        goal="accepted and executed",
        task_id="task-accepted-executed",
        status=TaskStatus.DONE,
        created_at="2026-01-14T19:30:00+00:00",
        metadata={
            "execution_status": "completed",
            "execution_result": "release verified",
        },
    )
    domain.create_task(
        conversation_id=conversation_id,
        goal="created before, updated inside",
        task_id="task-updated",
        created_at="2026-01-14T15:00:00+00:00",
        metadata={"execution_status": "completed"},
    )
    with domain.db.connect("core") as connection:
        connection.execute(
            "UPDATE tasks SET updated_at = %s WHERE task_id = 'task-updated'",
            ("2026-01-14T20:00:00+00:00",),
        )
    domain.create_task(
        conversation_id=conversation_id,
        goal="outside the window",
        task_id="task-outside",
        created_at="2026-01-15T17:00:00+00:00",
        metadata={"execution_status": "completed"},
    )

    domain.create_run(
        conversation_id=conversation_id,
        status=RunStatus.COMPLETED,
        run_id="run-completed",
        prompt="completed run",
        result="wrote the report",
        created_at="2026-01-14T21:00:00+00:00",
    )
    domain.create_run(
        conversation_id=conversation_id,
        status=RunStatus.FAILED,
        run_id="run-failed",
        prompt="failed run",
        error="provider_error",
        created_at="2026-01-14T22:00:00+00:00",
    )
    domain.create_run(
        conversation_id=conversation_id,
        status=RunStatus.COMPLETED,
        run_id="run-development",
        mode="development",
        prompt="implement the change",
        result="changed the code",
        created_at="2026-01-14T23:00:00+00:00",
    )

    review_input = _collector(store, workspace, domain=domain).collect_daily(DAY, TZ)
    by_id = {source.source_id: source for source in review_input.sources}

    assert by_id["task-completed"].may_prove_completion is False
    assert by_id["task-completed"].trust is SourceTrust.SYSTEM_RECEIPT
    assert by_id["task-completed"].verification == (
        "business_status=todo; execution_status=completed; execution_only"
    )
    assert "published release" in by_id["task-completed"].snapshot

    assert by_id["task-failed"].may_prove_completion is False
    assert by_id["task-failed"].verification == (
        "business_status=todo; execution_status=failed"
    )
    assert "tests failed" in by_id["task-failed"].snapshot

    assert by_id["task-done-only"].may_prove_completion is True
    assert by_id["task-done-only"].verification == (
        "business_status=done; execution_status=missing"
    )
    assert '"accepted_by_operator":true' in by_id["task-done-only"].snapshot

    assert by_id["task-accepted-executed"].may_prove_completion is True
    assert by_id["task-accepted-executed"].verification == (
        "business_status=done; execution_status=completed"
    )
    assert "release verified" in by_id["task-accepted-executed"].snapshot

    assert by_id["task-updated"].may_prove_completion is False
    assert by_id["task-updated"].verification == (
        "business_status=todo; execution_status=completed; execution_only"
    )
    assert "task-outside" not in by_id

    assert by_id["run-completed"].may_prove_completion is False
    assert by_id["run-completed"].source_type == "conversation"
    assert by_id["run-completed"].verification == (
        "conversation_turn; run_status=completed"
    )
    assert "wrote the report" in by_id["run-completed"].snapshot
    assert by_id["run-failed"].may_prove_completion is False
    assert by_id["run-failed"].source_type == "conversation"
    assert by_id["run-failed"].verification == "conversation_turn; run_status=failed"
    assert by_id["run-development"].source_type == "development_run"
    assert by_id["run-development"].verification == (
        "run_status=completed; execution_only"
    )
    assert review_input.completion_source_ids == {
        "task-done-only",
        "task-accepted-executed",
    }

    assert domain.get_task("task-failed").metadata["execution_status"] == "failed"  # type: ignore[union-attr]
    assert domain.get_run("run-failed").status is RunStatus.FAILED  # type: ignore[union-attr]


def test_conversations_new_documents_and_recall_are_collected(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    knowledge = KnowledgeStore(database_for(tmp_path))
    domain, conversation_id = _domain(tmp_path, workspace)
    documents = DocumentStore(database_for(tmp_path), tmp_path / "library")

    domain.create_run(
        conversation_id=conversation_id,
        status=RunStatus.COMPLETED,
        run_id="run-conversation",
        prompt="PostgreSQL backup",
        result="nightly dump",
        source=ConversationSource.WEB,
        created_at="2026-01-14T17:00:00+00:00",
    )
    domain.create_run(
        conversation_id=conversation_id,
        status=RunStatus.QUEUED,
        run_id="run-queued",
        prompt="still waiting",
        source=ConversationSource.WEB,
        created_at="2026-01-14T17:10:00+00:00",
    )
    domain.create_run(
        conversation_id=conversation_id,
        status=RunStatus.COMPLETED,
        run_id="run-scheduled",
        prompt="scheduled prompt",
        source=ConversationSource.SCHEDULE,
        created_at="2026-01-14T17:20:00+00:00",
    )

    old_document = _ready_document(
        documents,
        "Old operations notes",
        "PostgreSQL backup ops notebook uses a nightly dump.",
        created_at="2026-01-13T00:00:00+00:00",
    )
    new_document = _ready_document(
        documents,
        "PostgreSQL ops notebook",
        "New backup checklist for the team.",
        created_at="2026-01-14T18:00:00+00:00",
    )

    review_input = _collector(
        knowledge,
        workspace,
        domain=domain,
        documents=documents,
        searcher=DocumentSearcher(documents),
    ).collect_daily(DAY, TZ)
    by_id = {source.source_id: source for source in review_input.sources}

    assert "run-queued" not in by_id
    assert "run-scheduled" not in by_id
    conversation = by_id["run-conversation"]
    assert conversation.source_type == "conversation"
    assert conversation.trust is SourceTrust.SYSTEM_RECEIPT
    assert conversation.may_prove_completion is False
    snapshot = json.loads(conversation.snapshot)
    assert snapshot["prompt"] == "PostgreSQL backup"
    assert snapshot["result"] == "nightly dump"
    assert snapshot["conversation_title"] == "review sources"
    assert snapshot["source"] == ConversationSource.WEB.value

    document_ids = {
        source.source_id
        for source in review_input.sources
        if source.source_type == "knowledge_document"
    }
    assert document_ids == {new_document}
    assert old_document not in document_ids
    new_source = by_id[new_document]
    assert new_source.trust is SourceTrust.USER_PROVIDED
    assert new_source.may_prove_completion is False
    assert "New backup checklist for the team." in new_source.snapshot
    assert json.loads(new_source.snapshot)["status"] == DOCUMENT_READY

    recall_sources = tuple(
        source
        for source in review_input.sources
        if source.source_type == "knowledge_recall"
    )
    assert len(recall_sources) == 1
    recalled = recall_sources[0]
    assert recalled.source_id.startswith(f"recall-{old_document}-")
    assert recalled.trust is SourceTrust.USER_PROVIDED
    assert recalled.may_prove_completion is False
    recall_payload = json.loads(recalled.snapshot)
    assert recall_payload["document_id"] == old_document
    assert recall_payload["matched_by"]
    assert recall_payload["query"].startswith("PostgreSQL backup")
    assert "Old operations notes" in recalled.snapshot

    status_by_type = {
        status.source_type: status for status in review_input.collection_status
    }
    assert status_by_type["conversation"].discovered == 1
    assert status_by_type["knowledge_document"].discovered == 1
    assert status_by_type["knowledge_recall"].discovered == 1
    assert status_by_type["knowledge_recall"].status is SourceCollectionState.OK


def test_memory_entries_keep_local_time_and_explicit_trust(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    store = KnowledgeStore(database_for(tmp_path))
    memory = PersonalMemoryStore(tmp_path / "agent")
    memory.write_daily(
        DAY,
        "# 2026-01-15\n\n"
        "## 09:30\n"
        "- source: user_explicit\n"
        "- kind: learning\n"
        "Read the SQLite WAL documentation.\n\n"
        "## 10:15\n"
        "- source: agent_inferred\n"
        "- kind: activity\n"
        "Cairn observed a background task.\n\n"
        "## no-time\n"
        "- source: user_explicit\n"
        "This block has no valid local time.\n",
    )

    review_input = _collector(store, workspace, memory=memory).collect_daily(DAY, TZ)
    memory_sources = {
        source.occurred_at: source
        for source in review_input.sources
        if source.source_type == "personal_memory"
    }
    assert set(memory_sources) == {
        "2026-01-15T09:30:00+08:00",
        "2026-01-15T10:15:00+08:00",
    }
    explicit = memory_sources["2026-01-15T09:30:00+08:00"]
    inferred = memory_sources["2026-01-15T10:15:00+08:00"]
    assert explicit.trust is SourceTrust.USER_RECORDED
    assert explicit.verification == "user_recorded"
    assert explicit.may_prove_completion is False
    assert "SQLite WAL" in explicit.snapshot

    assert inferred.trust is SourceTrust.AGENT_INFERRED
    assert inferred.verification == "agent_inferred"
    assert inferred.may_prove_completion is False

    status = next(
        item
        for item in review_input.collection_status
        if item.source_type == "personal_memory"
    )
    assert status.status is SourceCollectionState.PARTIAL
    assert status.discovered == 3
    assert status.included == 2
    assert status.omitted == 1
    assert any("no valid local HH:MM" in note for note in status.notes)

    disabled = _collector(
        store, workspace, memory=memory, memory_enabled=False
    ).collect_daily(DAY, TZ)
    disabled_status = next(
        item
        for item in disabled.collection_status
        if item.source_type == "personal_memory"
    )
    assert disabled_status.status is SourceCollectionState.UNAVAILABLE
    assert not any(
        source.source_type == "personal_memory" for source in disabled.sources
    )

    other_day = _collector(store, workspace, memory=memory).collect_daily(
        date(2026, 1, 16), TZ
    )
    other_status = next(
        item
        for item in other_day.collection_status
        if item.source_type == "personal_memory"
    )
    assert other_status.status is SourceCollectionState.EMPTY


def test_budget_omissions_stay_in_manifest_with_original_identity(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    store = KnowledgeStore(database_for(tmp_path))
    first = _clip_at(
        store,
        "newest " + "x" * 400,
        "2026-01-15T10:00:00+00:00",
        request_id="newest",
    )
    second = _clip_at(
        store,
        "older " + "y" * 400,
        "2026-01-15T09:00:00+00:00",
        request_id="older",
    )
    memory = PersonalMemoryStore(tmp_path / "agent")
    memory.write_daily(
        DAY,
        "## 08:00\n- source: user_explicit\nA memory entry that will not fit. "
        + "z" * 400
        + "\n",
    )

    review_input = _collector(
        store, workspace, memory=memory, max_chars=1_000
    ).collect_daily(DAY, TZ)
    by_id = {source.source_id: source for source in review_input.sources}
    assert by_id[first].included is True
    assert by_id[second].included is False
    assert by_id[second].omitted_reason == "input_budget_exhausted"
    assert by_id[second].snapshot == ""
    memory_source = next(
        source
        for source in review_input.sources
        if source.source_type == "personal_memory"
    )
    assert memory_source.included is False

    manifest = {
        entry.source_id: entry
        for entry in (source.manifest_entry() for source in review_input.sources)
    }
    original = store.get(second)
    assert original is not None
    assert manifest[second].content_hash == original.content_hash
    assert manifest[second].included is False
    assert manifest[second].omitted_reason == "input_budget_exhausted"

    clip_status = next(
        item
        for item in review_input.collection_status
        if item.source_type == "knowledge_clip"
    )
    assert clip_status.status is SourceCollectionState.PARTIAL
    assert clip_status.discovered == 2
    assert clip_status.included == 1
    assert clip_status.omitted == 1
    assert review_input.used_chars == sum(
        source.prompt_chars() for source in review_input.included_sources
    )
    assert review_input.used_chars > sum(
        len(source.snapshot) for source in review_input.included_sources
    )
    assert review_input.used_chars <= review_input.budget_chars
    assert store.get(second).content == "older " + "y" * 400  # type: ignore[union-attr]


def test_input_fingerprint_is_deterministic_and_tracks_changes(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    store = KnowledgeStore(database_for(tmp_path))
    clip_id = _clip_at(
        store,
        "first content",
        "2026-01-15T10:00:00+00:00",
        request_id="fingerprint",
    )
    collector = _collector(store, workspace)
    first = collector.collect_daily(DAY, TZ)
    second = collector.collect_daily(DAY, TZ)
    assert first.input_fingerprint == second.input_fingerprint
    assert [source.source_id for source in first.sources] == [
        source.source_id for source in second.sources
    ]

    store.update(clip_id, content="changed content", source="web", tags=["test"])
    with store.db.connect("knowledge") as connection:
        connection.execute(
            "UPDATE clips SET updated_at = %s WHERE clip_id = %s",
            ("2026-01-15T11:00:00+00:00", clip_id),
        )
    changed = collector.collect_daily(DAY, TZ)
    assert changed.input_fingerprint != first.input_fingerprint
    assert changed.sources[0].content_hash != first.sources[0].content_hash


def test_empty_day_reports_each_source_family_without_fabrication(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    store = KnowledgeStore(database_for(tmp_path))
    domain, _ = _domain(tmp_path, workspace)
    memory = PersonalMemoryStore(tmp_path / "agent")

    review_input = _collector(
        store, workspace, domain=domain, memory=memory
    ).collect_daily(DAY, TZ)
    assert review_input.sources == ()
    assert review_input.included_sources == ()
    statuses = {
        item.source_type: item.status for item in review_input.collection_status
    }
    assert statuses["knowledge_clip"] is SourceCollectionState.EMPTY
    assert statuses["knowledge_favorites"] is SourceCollectionState.UNAVAILABLE
    assert statuses["development_task"] is SourceCollectionState.EMPTY
    assert statuses["development_run"] is SourceCollectionState.EMPTY
    assert statuses["personal_memory"] is SourceCollectionState.EMPTY


def test_invalid_timezone_is_rejected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    store = KnowledgeStore(database_for(tmp_path))
    collector = _collector(store, workspace)
    with pytest.raises(ValueError):
        collector.collect_daily(DAY, "Mars/Olympus")
    with pytest.raises(ValueError):
        collector.collect_daily(DAY, "  ")


def test_cutoff_window_is_contiguous_and_carries_late_sources_forward(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    store = KnowledgeStore(database_for(tmp_path))
    memory = PersonalMemoryStore(tmp_path / "agent")
    previous_late = _clip_at(
        store,
        "previous day after cutoff",
        "2026-01-14T13:00:00+00:00",
        request_id="previous-late",
    )
    current_early = _clip_at(
        store,
        "current day before cutoff",
        "2026-01-15T12:29:59+00:00",
        request_id="current-early",
    )
    current_late = _clip_at(
        store,
        "current day after cutoff",
        "2026-01-15T13:00:00+00:00",
        request_id="current-late",
    )
    memory.write_daily(
        date(2026, 1, 14),
        "# 2026-01-14\n\n"
        "## 19:00\n- source: user_explicit\nBefore the window.\n\n"
        "## 21:00\n- source: user_explicit\nPrevious day late entry.\n",
    )
    memory.write_daily(
        date(2026, 1, 15),
        "# 2026-01-15\n\n"
        "## 20:29\n- source: user_explicit\nBefore today's cutoff.\n\n"
        "## 20:30\n- source: user_explicit\nAt the cutoff.\n",
    )

    review_input = _collector(store, workspace, memory=memory).collect_daily(
        DAY, TZ, cutoff=time(20, 30)
    )
    ids = {
        source.source_id
        for source in review_input.sources
        if source.source_type == "knowledge_clip"
    }
    assert ids == {previous_late, current_early}
    assert current_late not in ids
    assert review_input.period_start.astimezone(UTC).isoformat() == (
        "2026-01-14T12:30:00+00:00"
    )
    assert review_input.period_end.astimezone(UTC).isoformat() == (
        "2026-01-15T12:30:00+00:00"
    )
    memory_times = {
        source.occurred_at
        for source in review_input.sources
        if source.source_type == "personal_memory"
    }
    assert memory_times == {
        "2026-01-14T21:00:00+08:00",
        "2026-01-15T20:29:00+08:00",
    }

    next_input = _collector(store, workspace, memory=memory).collect_daily(
        date(2026, 1, 16), TZ, cutoff=time(20, 30)
    )
    next_ids = {
        source.source_id
        for source in next_input.sources
        if source.source_type == "knowledge_clip"
    }
    assert current_late in next_ids
