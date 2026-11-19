"""A small but complete legacy SQLite/JSON state tree for import tests.

The tables mirror the schema version the previous release wrote; the importer
reads these files read-only and moves them into PostgreSQL.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from cairn.knowledge.models import content_hash
from cairn.scheduling.models import ScheduleJob, ScheduleKind

NOW = "2026-01-01T00:00:00.000000Z"
DIGEST = "0" * 64
CREDENTIAL = "cred-" + "1" * 32
SECRET = "legacy-bot-token-value"


@contextmanager
def _database(path: Path, version: int) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        yield connection
        connection.execute(f"PRAGMA user_version={version}")
        connection.commit()
    finally:
        connection.close()


def write_legacy_state(root: Path) -> Path:
    _write_domain(root / "runtime" / "domain.sqlite3")
    _write_sessions(root / "runtime" / "sessions" / "sessions.sqlite3")
    _write_knowledge(root / "knowledge" / "knowledge.sqlite3")
    _write_channels(root / "channels" / "channels.sqlite3")
    _write_operations(root)
    return root


def _write_domain(path: Path) -> None:
    with _database(path, 1) as db:
        db.executescript(
            """
            CREATE TABLE conversations (
                conversation_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL,
                source TEXT NOT NULL, initial_prompt TEXT NOT NULL, title TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                archived_at TEXT, metadata_json TEXT NOT NULL);
            CREATE TABLE wechat_bindings (
                workspace_id TEXT NOT NULL, account_digest TEXT NOT NULL,
                sender_digest TEXT NOT NULL, conversation_id TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE channel_bindings (
                workspace_id TEXT NOT NULL, connection_id TEXT NOT NULL,
                platform TEXT NOT NULL, binding_digest TEXT NOT NULL,
                conversation_id TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL);
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL, title TEXT NOT NULL,
                goal TEXT NOT NULL, status TEXT NOT NULL, issue_url TEXT,
                worktree TEXT, branch TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, metadata_json TEXT NOT NULL);
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                task_id TEXT, workspace_id TEXT NOT NULL, status TEXT NOT NULL,
                error TEXT, budget_json TEXT NOT NULL, trace_ref TEXT,
                session_id TEXT, checkpoint_receipt_json TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                ended_at TEXT, metadata_json TEXT NOT NULL);
            CREATE TABLE run_events (
                run_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                kind TEXT NOT NULL, data_json TEXT NOT NULL,
                created_at TEXT NOT NULL);
            CREATE TABLE conversation_requests (
                workspace_id TEXT NOT NULL, request_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL, fingerprint TEXT NOT NULL);
            CREATE TABLE run_requests (
                conversation_id TEXT NOT NULL, request_id TEXT NOT NULL,
                run_id TEXT NOT NULL, fingerprint TEXT NOT NULL);
            CREATE TABLE development_handoffs (
                run_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                request_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
                runtime_task_id TEXT, domain_task_id TEXT, prompt TEXT NOT NULL,
                source TEXT NOT NULL, diagnostic TEXT,
                history_origins_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL);
            CREATE TABLE handoff_reviews (
                request_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                action TEXT NOT NULL, expected_updated_at TEXT NOT NULL,
                note TEXT NOT NULL, status TEXT NOT NULL, result_status TEXT,
                diagnostic TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL);
            CREATE TABLE domain_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        db.execute(
            "INSERT INTO conversations VALUES (?,?,?,?,?,?,?,?,?)",
            ("conv-1", "ws-1", "cli", "hello", None, NOW, NOW, None, '{"k":"v"}'),
        )
        db.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "task-1",
                "conv-1",
                "ws-1",
                "title",
                "goal",
                "todo",
                None,
                None,
                None,
                NOW,
                NOW,
                "{}",
            ),
        )
        db.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "run-1",
                "conv-1",
                "task-1",
                "ws-1",
                "completed",
                None,
                "{}",
                None,
                None,
                None,
                NOW,
                NOW,
                NOW,
                '{"x":1}',
            ),
        )
        db.executemany(
            "INSERT INTO run_events VALUES (?,?,?,?,?)",
            [
                ("run-1", 1, "status", '{"state":"running"}', NOW),
                ("run-1", 2, "status", '{"state":"completed"}', NOW),
            ],
        )
        db.execute(
            "INSERT INTO conversation_requests VALUES (?,?,?,?)",
            ("ws-1", "req-1", "conv-1", "fp"),
        )
        db.execute(
            "INSERT INTO development_handoffs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "run-1",
                "conv-1",
                "handoff-1",
                "completed",
                None,
                "task-1",
                "prompt",
                "cli",
                None,
                "[]",
                NOW,
                NOW,
                "{}",
            ),
        )
        db.execute(
            "INSERT INTO handoff_reviews VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "review-req-1",
                "run-1",
                "accept",
                NOW,
                "note",
                "completed",
                "completed",
                None,
                NOW,
                NOW,
            ),
        )
        db.execute("INSERT INTO domain_meta VALUES ('cursor', '42')")


def _write_sessions(path: Path) -> None:
    with _database(path, 4) as db:
        db.executescript(
            """
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY, version INTEGER NOT NULL,
                head_seq INTEGER NOT NULL, state TEXT NOT NULL,
                active_turn_id TEXT, binding_json TEXT NOT NULL,
                created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL);
            CREATE TABLE turns (
                session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                base_seq INTEGER NOT NULL, task_id TEXT, state TEXT NOT NULL,
                input_digest TEXT NOT NULL, started_at_utc TEXT NOT NULL,
                ended_at_utc TEXT, settled_seq INTEGER,
                diagnostics_json TEXT NOT NULL);
            CREATE TABLE checkpoints (
                session_id TEXT NOT NULL, seq INTEGER NOT NULL, turn_id TEXT,
                format_version INTEGER NOT NULL, created_at_utc TEXT NOT NULL,
                payload BLOB NOT NULL, sha256 TEXT NOT NULL);
            CREATE TABLE tool_fences (
                session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL, assistant_message_index INTEGER NOT NULL,
                call_id TEXT NOT NULL, tool_name TEXT NOT NULL,
                observed_at_utc TEXT NOT NULL);
            CREATE TABLE tasks (
                session_id TEXT NOT NULL, task_id TEXT NOT NULL,
                version INTEGER NOT NULL, phase TEXT NOT NULL,
                base_seq INTEGER NOT NULL, checkpoint_seq INTEGER,
                record_json TEXT NOT NULL);
            """
        )
        db.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
            ("session-1", 1, 1, "ready", None, "{}", NOW, NOW),
        )
        db.execute(
            "INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?)",
            ("session-1", 0, None, 1, NOW, b"{}", DIGEST),
        )
        db.execute(
            "INSERT INTO turns VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("session-1", "turn-1", 0, None, "completed", DIGEST, NOW, NOW, 1, "{}"),
        )
        db.execute(
            "INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?)",
            ("session-1", 1, "turn-1", 1, NOW, b"{}", DIGEST),
        )
        db.execute(
            "INSERT INTO tool_fences VALUES (?,?,?,?,?,?,?)",
            ("session-1", "turn-1", 1, 0, "call-1", "bash", NOW),
        )
        db.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?,?)",
            ("session-1", "task-1", 0, "result_recorded", 0, 1, "{}"),
        )


def _write_knowledge(path: Path) -> None:
    with _database(path, 5) as db:
        db.executescript(
            """
            CREATE TABLE clips (
                clip_id TEXT PRIMARY KEY, content TEXT NOT NULL,
                source TEXT NOT NULL, tags_json TEXT NOT NULL,
                content_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, archived_at TEXT);
            CREATE TABLE clip_requests (
                request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                clip_id TEXT NOT NULL);
            CREATE TABLE reviews (
                review_id TEXT PRIMARY KEY, review_type TEXT NOT NULL,
                review_date TEXT NOT NULL, period_start TEXT NOT NULL,
                period_end TEXT NOT NULL, timezone TEXT NOT NULL,
                status TEXT NOT NULL, source_manifest_json TEXT NOT NULL,
                collection_status_json TEXT NOT NULL,
                source_snapshots_json TEXT NOT NULL DEFAULT '[]',
                input_fingerprint TEXT NOT NULL, summary_json TEXT,
                summary_markdown TEXT, model_id TEXT,
                generation_attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                completed_at TEXT, error TEXT,
                version INTEGER NOT NULL DEFAULT 1,
                supersedes_review_id TEXT);
            CREATE TABLE review_requests (
                request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                review_id TEXT NOT NULL);
            CREATE TABLE review_deliveries (
                delivery_id TEXT PRIMARY KEY, review_id TEXT NOT NULL UNIQUE,
                review_version INTEGER NOT NULL, channel TEXT NOT NULL,
                account_id TEXT NOT NULL DEFAULT '', recipient TEXT NOT NULL
                DEFAULT '', status TEXT NOT NULL,
                attempt_id TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                context_token TEXT NOT NULL DEFAULT '', last_error TEXT,
                created_at TEXT NOT NULL, attempted_at TEXT, completed_at TEXT,
                updated_at TEXT NOT NULL);
            CREATE TABLE review_delivery_requests (
                request_id TEXT PRIMARY KEY, review_id TEXT NOT NULL,
                delivery_id TEXT NOT NULL DEFAULT '', action TEXT NOT NULL,
                rebind INTEGER NOT NULL DEFAULT 0, outcome TEXT NOT NULL,
                error TEXT, created_at TEXT NOT NULL);
            """
        )
        db.execute(
            "INSERT INTO clips VALUES (?,?,?,?,?,?,?,?)",
            (
                "clip-1",
                "content",
                "note",
                '["a","b"]',
                content_hash("content"),
                NOW,
                NOW,
                None,
            ),
        )
        db.execute(
            "INSERT INTO clip_requests VALUES (?,?,?)", ("req-clip", "fp", "clip-1")
        )
        db.execute(
            "INSERT INTO reviews VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "review-1",
                "weekly",
                "2026-01-01",
                NOW,
                NOW,
                "Asia/Shanghai",
                "completed",
                "[]",
                "[]",
                "[]",
                DIGEST,
                '{"week_id":"2026-W01","period_start":"2026-01-01T00:00:00Z",'
                '"period_end":"2026-01-07T00:00:00Z","overview":"done"}',
                "# s",
                "model-1",
                1,
                NOW,
                NOW,
                NOW,
                None,
                1,
                None,
            ),
        )
        db.execute(
            "INSERT INTO review_requests VALUES (?,?,?)",
            ("req-review", "fp", "review-1"),
        )
        db.execute(
            "INSERT INTO review_deliveries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "delivery-1",
                "review-1",
                1,
                "wechat",
                "account-1",
                "owner",
                "completed",
                "attempt-1",
                1,
                "",
                None,
                NOW,
                NOW,
                NOW,
                NOW,
            ),
        )
        db.execute(
            "INSERT INTO review_delivery_requests VALUES (?,?,?,?,?,?,?,?)",
            (
                "req-delivery",
                "review-1",
                "delivery-1",
                "send",
                0,
                "delivered",
                None,
                NOW,
            ),
        )


def _write_channels(path: Path) -> None:
    with _database(path, 5) as db:
        db.executescript(
            """
            CREATE TABLE channel_connections (
                connection_id TEXT PRIMARY KEY, platform TEXT NOT NULL,
                display_name TEXT NOT NULL, enabled INTEGER NOT NULL,
                desired_state TEXT NOT NULL, credential_ref TEXT,
                owner_identity TEXT, allowed_senders_json TEXT NOT NULL
                DEFAULT '[]', metadata_json TEXT NOT NULL,
                revision INTEGER NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL);
            CREATE TABLE channel_credentials (
                credential_ref TEXT PRIMARY KEY, secret_json TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE channel_requests (
                request_id TEXT PRIMARY KEY, operation TEXT NOT NULL,
                digest TEXT NOT NULL, state TEXT NOT NULL, result_json TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE channel_inbound (
                platform TEXT NOT NULL, connection_id TEXT NOT NULL,
                external_message_id TEXT NOT NULL, event_digest TEXT NOT NULL,
                state TEXT NOT NULL, conversation_id TEXT, run_id TEXT,
                conversation_binding TEXT NOT NULL DEFAULT '',
                sender_id TEXT NOT NULL DEFAULT '',
                sender_role TEXT NOT NULL DEFAULT '',
                adapter_identity TEXT NOT NULL DEFAULT '',
                request_id TEXT NOT NULL DEFAULT '', error TEXT,
                received_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE channel_inbound_inbox (
                connection_id TEXT NOT NULL, adapter_identity TEXT NOT NULL,
                external_message_id TEXT NOT NULL, platform TEXT NOT NULL,
                event_digest TEXT NOT NULL, sender_id TEXT NOT NULL,
                conversation_binding TEXT NOT NULL, text TEXT NOT NULL,
                received_at TEXT NOT NULL, state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
                next_attempt_at REAL, deadline_at REAL NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE channel_deliveries (
                delivery_id TEXT PRIMARY KEY, connection_id TEXT NOT NULL,
                platform TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                target_digest TEXT NOT NULL, status TEXT NOT NULL, error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE channel_replies (
                reply_id TEXT PRIMARY KEY, connection_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL, run_id TEXT NOT NULL UNIQUE,
                target TEXT NOT NULL, external_message_id TEXT NOT NULL,
                sender_id TEXT NOT NULL DEFAULT '',
                sender_role TEXT NOT NULL DEFAULT '',
                adapter_identity TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE channel_adapter_state (
                connection_id TEXT NOT NULL, key TEXT NOT NULL,
                value_json TEXT NOT NULL, updated_at TEXT NOT NULL);
            """
        )
        db.execute(
            "INSERT INTO channel_connections VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "conn-1",
                "wechat",
                "WeChat",
                1,
                "running",
                CREDENTIAL,
                "owner",
                '["owner"]',
                '{"a":1}',
                3,
                NOW,
                NOW,
            ),
        )
        db.execute(
            "INSERT INTO channel_credentials VALUES (?,?,?,?)",
            (
                CREDENTIAL,
                json.dumps(
                    {"bot_token": SECRET, "base_url": "https://example.test"},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                NOW,
                NOW,
            ),
        )
        db.execute(
            "INSERT INTO channel_requests VALUES (?,?,?,?,?,?,?)",
            ("creq-1", "create", "digest", "completed", '{"ok":true}', NOW, NOW),
        )
        db.execute(
            "INSERT INTO channel_inbound VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "wechat",
                "conn-1",
                "msg-1",
                "digest",
                "completed",
                "conv-1",
                "run-1",
                "binding",
                "sender",
                "owner",
                "adapter",
                "req-in",
                None,
                NOW,
                NOW,
            ),
        )
        db.execute(
            "INSERT INTO channel_inbound_inbox VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "conn-1",
                "adapter",
                "msg-2",
                "wechat",
                "digest",
                "sender",
                "binding",
                "text",
                NOW,
                "done",
                1,
                None,
                1.5,
                2.5,
                NOW,
                NOW,
            ),
        )
        db.execute(
            "INSERT INTO channel_deliveries VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "delivery-1",
                "conn-1",
                "wechat",
                "key",
                "target",
                "completed",
                None,
                NOW,
                NOW,
            ),
        )
        db.execute(
            "INSERT INTO channel_replies VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "reply-1",
                "conn-1",
                "conv-1",
                "run-1",
                "target",
                "msg-out",
                "sender",
                "owner",
                "adapter",
                "completed",
                1,
                None,
                NOW,
                NOW,
            ),
        )
        db.execute(
            "INSERT INTO channel_adapter_state VALUES (?,?,?,?)",
            ("conn-1", "cursor", '{"offset":2}', NOW),
        )


def _write_operations(root: Path) -> None:
    schedule = root / "schedule"
    schedule.mkdir(parents=True)
    created = datetime(2026, 1, 1, tzinfo=UTC)
    job = ScheduleJob(
        job_id="job-1",
        prompt="scheduled prompt",
        workspace="/workspace",
        recipient="owner",
        account_id="account-1",
        kind=ScheduleKind.ONCE,
        at=created,
        created_at=created,
        next_run_at=created,
    )
    (schedule / "jobs.json").write_text(
        json.dumps({"version": 1, "jobs": [job.model_dump(mode="json")]}),
        encoding="utf-8",
    )
    (schedule / "runs.jsonl").write_text(
        json.dumps({"job_id": "job-1", "status": "completed"}) + "\n",
        encoding="utf-8",
    )
    (root / "knowledge").mkdir(parents=True, exist_ok=True)
    (root / "knowledge" / "review-schedule.json").write_text(
        json.dumps({"version": 1, "initialized": True}), encoding="utf-8"
    )
    (root / "knowledge" / "weekly-review-schedule.json").write_text(
        json.dumps({"version": 1}), encoding="utf-8"
    )
    runtime = root / "runtime"
    (runtime / "runtime.json").write_text(
        json.dumps({"version": 1, "tasks": {}, "events": {}}), encoding="utf-8"
    )
