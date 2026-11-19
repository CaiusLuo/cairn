"""Offline import of a stopped host's SQLite/JSON state into PostgreSQL.

The legacy files are opened read-only and never rewritten. Rows are prepared
first, inserted in one transaction, then re-read from the target and compared
per table by row count and a sha256 over canonical rows. A failed comparison
rolls the whole import back.

Usage::

    cairn-pg-import --state ~/.local/state/cairn --database-url postgresql:///cairn
    cairn-pg-import --state ~/.local/state/cairn --dry-run
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import psycopg
from psycopg import sql as psql
from pydantic import BaseModel, ConfigDict

from cairn.channels.credentials import CredentialVault
from cairn.development.catalogue import CatalogueData
from cairn.review.schedule import REVIEW_SCHEDULE_VERSION
from cairn.review.weekly_schedule import WEEKLY_SCHEDULE_VERSION
from cairn.scheduling.models import ScheduleJob
from cairn.shared.db import Database

_COMPONENT_VERSION = {"domain": 1, "sessions": 4, "knowledge": 5, "channel": 5}
_CONNECTION_COLUMNS = (
    "connection_id",
    "platform",
    "display_name",
    "enabled",
    "desired_state",
    "credential_ref",
    "credential_created_at",
    "owner_identity",
    "allowed_senders_json",
    "metadata_json",
    "revision",
    "created_at",
    "updated_at",
)
_CONNECTION_SOURCE = (
    "connection_id, platform, display_name, enabled, desired_state, "
    "credential_ref, owner_identity, allowed_senders_json, metadata_json, "
    "revision, created_at, updated_at"
)
_CONNECTION_JSON = ("allowed_senders_json", "metadata_json")


@dataclass(frozen=True)
class _Table:
    component: str
    name: str
    domain: str
    columns: tuple[str, ...]
    json_columns: tuple[str, ...] = ()
    order_by: tuple[str, ...] = ()
    target_order_by: tuple[str, ...] = ()

    @property
    def identity(self) -> str:
        return f"{self.domain}.{self.name}"


_TABLES: tuple[_Table, ...] = (
    # core
    _Table(
        "domain",
        "conversations",
        "core",
        (
            "conversation_id",
            "workspace_id",
            "source",
            "initial_prompt",
            "title",
            "created_at",
            "updated_at",
            "archived_at",
            "metadata_json",
        ),
        json_columns=("metadata_json",),
        order_by=("conversation_id",),
    ),
    _Table(
        "domain",
        "wechat_bindings",
        "core",
        (
            "workspace_id",
            "account_digest",
            "sender_digest",
            "conversation_id",
            "created_at",
            "updated_at",
        ),
        order_by=("workspace_id", "account_digest", "sender_digest"),
    ),
    _Table(
        "domain",
        "channel_bindings",
        "core",
        (
            "workspace_id",
            "connection_id",
            "platform",
            "binding_digest",
            "conversation_id",
            "created_at",
            "updated_at",
        ),
        order_by=("workspace_id", "connection_id", "binding_digest"),
    ),
    _Table(
        "domain",
        "tasks",
        "core",
        (
            "task_id",
            "conversation_id",
            "workspace_id",
            "title",
            "goal",
            "status",
            "issue_url",
            "worktree",
            "branch",
            "created_at",
            "updated_at",
            "metadata_json",
        ),
        json_columns=("metadata_json",),
        order_by=("task_id",),
    ),
    _Table(
        "domain",
        "runs",
        "core",
        (
            "run_id",
            "conversation_id",
            "task_id",
            "workspace_id",
            "status",
            "error",
            "budget_json",
            "trace_ref",
            "session_id",
            "checkpoint_receipt_json",
            "created_at",
            "updated_at",
            "ended_at",
            "metadata_json",
        ),
        json_columns=("budget_json", "checkpoint_receipt_json", "metadata_json"),
        order_by=("run_id",),
    ),
    _Table(
        "domain",
        "run_events",
        "core",
        ("run_id", "sequence", "kind", "data_json", "created_at"),
        json_columns=("data_json",),
        order_by=("run_id", "sequence"),
    ),
    _Table(
        "domain",
        "conversation_requests",
        "core",
        ("workspace_id", "request_id", "conversation_id", "fingerprint"),
        order_by=("workspace_id", "request_id"),
    ),
    _Table(
        "domain",
        "run_requests",
        "core",
        ("conversation_id", "request_id", "run_id", "fingerprint"),
        order_by=("conversation_id", "request_id"),
    ),
    _Table(
        "domain",
        "development_handoffs",
        "core",
        (
            "run_id",
            "conversation_id",
            "request_id",
            "status",
            "runtime_task_id",
            "domain_task_id",
            "prompt",
            "source",
            "diagnostic",
            "history_origins_json",
            "created_at",
            "updated_at",
            "metadata_json",
        ),
        json_columns=("history_origins_json", "metadata_json"),
        order_by=("run_id",),
    ),
    _Table(
        "domain",
        "handoff_reviews",
        "core",
        (
            "request_id",
            "run_id",
            "action",
            "expected_updated_at",
            "note",
            "status",
            "result_status",
            "diagnostic",
            "created_at",
            "updated_at",
        ),
        order_by=("request_id",),
    ),
    _Table("domain", "domain_meta", "core", ("key", "value"), order_by=("key",)),
    # agent
    _Table(
        "sessions",
        "sessions",
        "agent",
        (
            "session_id",
            "version",
            "head_seq",
            "state",
            "active_turn_id",
            "binding_json",
            "created_at_utc",
            "updated_at_utc",
        ),
        order_by=("session_id",),
    ),
    _Table(
        "sessions",
        "checkpoints",
        "agent",
        (
            "session_id",
            "seq",
            "turn_id",
            "format_version",
            "created_at_utc",
            "payload",
            "sha256",
        ),
        order_by=("session_id", "seq"),
    ),
    _Table(
        "sessions",
        "turns",
        "agent",
        (
            "session_id",
            "turn_id",
            "base_seq",
            "task_id",
            "state",
            "input_digest",
            "started_at_utc",
            "ended_at_utc",
            "settled_seq",
            "diagnostics_json",
        ),
        # rowid preserves the append order the PostgreSQL identity copies.
        order_by=("session_id", "rowid"),
        target_order_by=("session_id", "seq"),
    ),
    _Table(
        "sessions",
        "tool_fences",
        "agent",
        (
            "session_id",
            "turn_id",
            "ordinal",
            "assistant_message_index",
            "call_id",
            "tool_name",
            "observed_at_utc",
        ),
        order_by=("session_id", "turn_id", "ordinal"),
    ),
    _Table(
        "sessions",
        "tasks",
        "agent",
        (
            "session_id",
            "task_id",
            "version",
            "phase",
            "base_seq",
            "checkpoint_seq",
            "record_json",
        ),
        order_by=("session_id", "task_id"),
    ),
    # knowledge
    _Table(
        "knowledge",
        "clips",
        "knowledge",
        (
            "clip_id",
            "content",
            "source",
            "tags_json",
            "content_hash",
            "created_at",
            "updated_at",
            "archived_at",
        ),
        json_columns=("tags_json",),
        order_by=("clip_id",),
    ),
    _Table(
        "knowledge",
        "clip_requests",
        "knowledge",
        ("request_id", "fingerprint", "clip_id"),
        order_by=("request_id",),
    ),
    _Table(
        "knowledge",
        "reviews",
        "knowledge",
        (
            "review_id",
            "review_type",
            "review_date",
            "period_start",
            "period_end",
            "timezone",
            "status",
            "source_manifest_json",
            "collection_status_json",
            "source_snapshots_json",
            "input_fingerprint",
            "summary_json",
            "summary_markdown",
            "model_id",
            "generation_attempts",
            "created_at",
            "updated_at",
            "completed_at",
            "error",
            "version",
            "supersedes_review_id",
        ),
        json_columns=(
            "source_manifest_json",
            "collection_status_json",
            "source_snapshots_json",
            "summary_json",
        ),
        order_by=("review_id",),
    ),
    _Table(
        "knowledge",
        "review_requests",
        "knowledge",
        ("request_id", "fingerprint", "review_id"),
        order_by=("request_id",),
    ),
    _Table(
        "knowledge",
        "review_deliveries",
        "knowledge",
        (
            "delivery_id",
            "review_id",
            "review_version",
            "channel",
            "account_id",
            "recipient",
            "status",
            "attempt_id",
            "attempts",
            "context_token",
            "last_error",
            "created_at",
            "attempted_at",
            "completed_at",
            "updated_at",
        ),
        order_by=("delivery_id",),
    ),
    _Table(
        "knowledge",
        "review_delivery_requests",
        "knowledge",
        (
            "request_id",
            "review_id",
            "delivery_id",
            "action",
            "rebind",
            "outcome",
            "error",
            "created_at",
        ),
        order_by=("request_id",),
    ),
    # channel
    _Table(
        "channel",
        "channel_requests",
        "channel",
        (
            "request_id",
            "operation",
            "digest",
            "state",
            "result_json",
            "created_at",
            "updated_at",
        ),
        json_columns=("result_json",),
        order_by=("request_id",),
    ),
    _Table(
        "channel",
        "channel_inbound",
        "channel",
        (
            "platform",
            "connection_id",
            "external_message_id",
            "event_digest",
            "state",
            "conversation_id",
            "run_id",
            "conversation_binding",
            "sender_id",
            "sender_role",
            "adapter_identity",
            "request_id",
            "error",
            "received_at",
            "updated_at",
        ),
        order_by=("connection_id", "adapter_identity", "external_message_id"),
    ),
    _Table(
        "channel",
        "channel_inbound_inbox",
        "channel",
        (
            "connection_id",
            "adapter_identity",
            "external_message_id",
            "platform",
            "event_digest",
            "sender_id",
            "conversation_binding",
            "text",
            "received_at",
            "state",
            "attempts",
            "last_error",
            "next_attempt_at",
            "deadline_at",
            "created_at",
            "updated_at",
        ),
        order_by=("connection_id", "adapter_identity", "external_message_id"),
    ),
    _Table(
        "channel",
        "channel_deliveries",
        "channel",
        (
            "delivery_id",
            "connection_id",
            "platform",
            "idempotency_key",
            "target_digest",
            "status",
            "error",
            "created_at",
            "updated_at",
        ),
        order_by=("delivery_id",),
    ),
    _Table(
        "channel",
        "channel_replies",
        "channel",
        (
            "reply_id",
            "connection_id",
            "conversation_id",
            "run_id",
            "target",
            "external_message_id",
            "sender_id",
            "sender_role",
            "adapter_identity",
            "state",
            "attempts",
            "error",
            "created_at",
            "updated_at",
        ),
        order_by=("reply_id",),
    ),
    _Table(
        "channel",
        "channel_adapter_state",
        "channel",
        ("connection_id", "key", "value_json", "updated_at"),
        json_columns=("value_json",),
        order_by=("connection_id", "key"),
    ),
)


class TableReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    table: str
    rows: int
    sha256: str
    verified: bool = False


class ImportReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dry_run: bool
    replaced: bool
    credentials: int = 0
    skipped_files: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    tables: tuple[TableReport, ...] = ()


@dataclass
class _Plan:
    table: _Table
    columns: tuple[str, ...]
    json_columns: tuple[str, ...]
    rows: list[tuple[Any, ...]]
    digest: str


def _canonical(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"hex": bytes(value).hex()}
    return value


def _json_text(value: Any) -> str:
    """Canonical JSON for a value that may be source text or target objects."""
    parsed = json.loads(value) if isinstance(value, str) else value
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(
    columns: Sequence[str],
    json_columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
) -> str:
    encoded = [
        [
            [
                column,
                _json_text(value)
                if column in json_columns and value is not None
                else _canonical(value),
            ]
            for column, value in zip(columns, row, strict=True)
        ]
        for row in rows
    ]
    payload = json.dumps(
        encoded, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _open_source(path: Path, version: int) -> sqlite3.Connection:
    if path.is_symlink():
        raise ValueError(f"Import refuses a symlink: {path}")
    uri = "file:" + quote(str(path)) + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute("PRAGMA user_version").fetchone()
        found = int(row[0])
        if found != version:
            raise ValueError(
                f"{path.name} has schema version {found}; expected {version}. "
                "Start the previous release once to upgrade the store, then retry."
            )
        check = connection.execute("PRAGMA quick_check").fetchone()
        if check is None or str(check[0]) != "ok":
            raise ValueError(f"{path.name} failed its integrity check")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError(f"{path.name} has broken foreign keys")
    except BaseException:
        connection.close()
        raise
    return connection


def _prepare(connection: sqlite3.Connection, table: _Table) -> _Plan:
    query = f"SELECT {', '.join(table.columns)} FROM {table.name}"
    if table.order_by:
        query += " ORDER BY " + ", ".join(table.order_by)
    rows = [
        tuple(
            _json_text(row[column])
            if column in table.json_columns and row[column] is not None
            else row[column]
            for column in table.columns
        )
        for row in connection.execute(query)
    ]
    return _Plan(
        table=table,
        columns=table.columns,
        json_columns=table.json_columns,
        rows=rows,
        digest=_digest(table.columns, table.json_columns, rows),
    )


def _connection_plan(
    connection: sqlite3.Connection,
    vault: CredentialVault | None,
    created: list[str],
) -> tuple[_Plan, int]:
    secrets: dict[str, tuple[Any, str]] = {
        str(row["credential_ref"]): (
            json.loads(row["secret_json"]),
            str(row["created_at"]),
        )
        for row in connection.execute(
            "SELECT credential_ref, secret_json, created_at FROM channel_credentials"
        )
    }
    table = _Table(
        "channel",
        "channel_connections",
        "channel",
        _CONNECTION_COLUMNS,
        json_columns=_CONNECTION_JSON,
        order_by=("connection_id",),
    )
    rows: list[tuple[Any, ...]] = []
    credentials = 0
    for row in connection.execute(
        f"SELECT {_CONNECTION_SOURCE} FROM channel_connections ORDER BY connection_id"
    ):
        values = {column: row[column] for column in _CONNECTION_SOURCE.split(", ")}
        old_ref = values["credential_ref"]
        created_at: str | None = None
        if old_ref is not None:
            if old_ref not in secrets:
                raise ValueError(
                    f"connection {values['connection_id']} references a missing secret"
                )
            secret, created_at = secrets[old_ref]
            if not isinstance(secret, dict) or not all(
                isinstance(key, str) and isinstance(item, str)
                for key, item in secret.items()
            ):
                raise ValueError(
                    f"connection {values['connection_id']} has an invalid secret"
                )
            credentials += 1
            if vault is not None:
                values["credential_ref"] = vault.create(secret, created_at)
                created.append(values["credential_ref"])
        values["credential_created_at"] = created_at
        rows.append(
            tuple(
                _json_text(values[column])
                if column in _CONNECTION_JSON and values[column] is not None
                else values[column]
                for column in _CONNECTION_COLUMNS
            )
        )
    return (
        _Plan(
            table=table,
            columns=_CONNECTION_COLUMNS,
            json_columns=_CONNECTION_JSON,
            rows=rows,
            digest=_digest(_CONNECTION_COLUMNS, _CONNECTION_JSON, rows),
        ),
        credentials,
    )


def _operations(
    state: Path, skipped: list[str]
) -> tuple[list[tuple[str, dict[str, Any]]], list[dict[str, Any]], list[str]]:
    documents: list[tuple[str, dict[str, Any]]] = []
    runs: list[dict[str, Any]] = []
    notes: list[str] = []

    def load_json(
        path: Path, *, version: int | None, name: str
    ) -> dict[str, Any] | None:
        if not path.is_file():
            if path.exists():
                raise ValueError(f"Import refuses a non-file: {path}")
            skipped.append(str(path))
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"{name} has an unsupported shape")
        if version is not None and payload.get("version") != version:
            raise ValueError(f"{name} has an unsupported shape")
        return payload

    jobs_path = state / "schedule" / "jobs.json"
    if jobs_path.is_file():
        payload = load_json(jobs_path, version=1, name="schedule jobs")
        assert payload is not None
        entries = payload.get("jobs")
        if not isinstance(entries, list):
            raise ValueError("schedule jobs are malformed")
        jobs = tuple(ScheduleJob.model_validate(item) for item in entries)
        if len({job.job_id for job in jobs}) != len(jobs):
            raise ValueError("schedule jobs contain duplicate ids")
        documents.append(("jobs", payload))
    else:
        load_json(jobs_path, version=None, name="schedule jobs")

    runs_path = state / "schedule" / "runs.jsonl"
    if runs_path.is_file():
        broken = 0
        for line in runs_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                broken += 1
                continue
            if isinstance(value, dict):
                runs.append(value)
            else:
                broken += 1
        if broken:
            notes.append(f"schedule runs: skipped {broken} malformed line(s)")
    else:
        load_json(runs_path, version=None, name="schedule runs")

    for name, filename, version in (
        ("review-schedule", "review-schedule.json", REVIEW_SCHEDULE_VERSION),
        (
            "weekly-review-schedule",
            "weekly-review-schedule.json",
            WEEKLY_SCHEDULE_VERSION,
        ),
    ):
        payload = load_json(state / "schedule" / filename, version=version, name=name)
        if payload is not None:
            documents.append((name, payload))

    catalogue_path = state / "runtime" / "runtime.json"
    if catalogue_path.is_file():
        raw = catalogue_path.read_text(encoding="utf-8")
        CatalogueData.model_validate_json(raw)
        documents.append(("catalogue", json.loads(raw)))
    else:
        load_json(catalogue_path, version=None, name="runtime catalogue")
    return documents, runs, notes


def _qualified(schema: str, name: str) -> Any:
    return psql.SQL("{}.{}").format(psql.Identifier(schema), psql.Identifier(name))


def _target_rows(
    connection: psycopg.Connection[Any],
    table: _Table,
    columns: Sequence[str],
    schemas: dict[str, str],
) -> list[tuple[Any, ...]]:
    query = psql.SQL("SELECT {} FROM {}").format(
        psql.SQL(", ").join(psql.Identifier(column) for column in columns),
        _qualified(schemas[table.domain], table.name),
    )
    order = table.target_order_by or table.order_by
    if order:
        query = (
            query
            + psql.SQL(" ORDER BY ")
            + psql.SQL(", ").join(psql.Identifier(column) for column in order)
        )
    return [tuple(row) for row in connection.execute(query).fetchall()]


def _insert(
    connection: psycopg.Connection[Any], plan: _Plan, schemas: dict[str, str]
) -> None:
    statement = psql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        _qualified(schemas[plan.table.domain], plan.table.name),
        psql.SQL(", ").join(psql.Identifier(column) for column in plan.columns),
        psql.SQL(", ").join(psql.SQL("%s") for _ in plan.columns),
    )
    for row in plan.rows:
        connection.execute(statement, list(row))


def _plans(plans: Sequence[_Plan], connections: _Plan | None) -> list[_Plan]:
    return [*plans, *([connections] if connections is not None else [])]


def _occupied(
    connection: psycopg.Connection[Any],
    plans: Sequence[_Plan],
    schemas: dict[str, str],
) -> list[str]:
    names: list[str] = []
    for plan in plans:
        query = psql.SQL("SELECT 1 FROM {} LIMIT 1").format(
            _qualified(schemas[plan.table.domain], plan.table.name)
        )
        if connection.execute(query).fetchone() is not None:
            names.append(plan.table.identity)
    return names


def _replace(
    connection: psycopg.Connection[Any],
    plans: Sequence[_Plan],
    schemas: dict[str, str],
) -> None:
    by_domain: dict[str, list[str]] = {}
    for plan in plans:
        by_domain.setdefault(plan.table.domain, []).append(plan.table.name)
    for domain, names in by_domain.items():
        connection.execute(
            psql.SQL("TRUNCATE TABLE {}").format(
                psql.SQL(", ").join(_qualified(schemas[domain], name) for name in names)
            )
        )


def _verify(
    connection: psycopg.Connection[Any], plan: _Plan, schemas: dict[str, str]
) -> TableReport:
    rows = _target_rows(connection, plan.table, plan.columns, schemas)
    digest = _digest(plan.columns, plan.json_columns, rows)
    if digest != plan.digest or len(rows) != len(plan.rows):
        raise ValueError(
            f"{plan.table.identity} verification failed: "
            f"source {len(plan.rows)} rows/{plan.digest[:12]}, "
            f"target {len(rows)} rows/{digest[:12]}"
        )
    return TableReport(
        table=plan.table.identity,
        rows=len(rows),
        sha256=digest,
        verified=True,
    )


def _ops_tables(schema: str) -> tuple[Any, Any]:
    return _qualified(schema, "schedule_state"), _qualified(schema, "schedule_runs")


def _ops_occupied(connection: psycopg.Connection[Any], schema: str) -> bool:
    state_table, runs_table = _ops_tables(schema)
    for table in (state_table, runs_table):
        if (
            connection.execute(
                psql.SQL("SELECT 1 FROM {} LIMIT 1").format(table)
            ).fetchone()
            is not None
        ):
            return True
    return False


def _ops_replace(connection: psycopg.Connection[Any], schema: str) -> None:
    state_table, runs_table = _ops_tables(schema)
    connection.execute(
        psql.SQL("TRUNCATE TABLE {}, {}").format(state_table, runs_table)
    )


def _write_documents(
    connection: psycopg.Connection[Any],
    schema: str,
    documents: Sequence[tuple[str, dict[str, Any]]],
    runs: Sequence[dict[str, Any]],
) -> list[TableReport]:
    reports: list[TableReport] = []
    state_table = _qualified(schema, "schedule_state")
    for key, payload in documents:
        text = _json_text(payload)
        connection.execute(
            psql.SQL("INSERT INTO {}(key, payload) VALUES (%s, %s)").format(
                state_table
            ),
            (key, text),
        )
        stored = connection.execute(
            psql.SQL("SELECT payload FROM {} WHERE key = %s").format(state_table),
            (key,),
        ).fetchone()
        if stored is None or _json_text(stored[0]) != text:
            raise ValueError(f"ops.schedule_state[{key}] verification failed")
        reports.append(
            TableReport(
                table=f"ops.schedule_state[{key}]",
                rows=1,
                sha256=_digest(("payload",), ("payload",), [(text,)]),
                verified=True,
            )
        )
    if runs:
        runs_table = _qualified(schema, "schedule_runs")
        statement = psql.SQL("INSERT INTO {}(record) VALUES (%s)").format(runs_table)
        for record in runs:
            connection.execute(statement, (_json_text(record),))
        stored = connection.execute(
            psql.SQL("SELECT record FROM {} ORDER BY id").format(runs_table)
        ).fetchall()
        expected = [_json_text(record) for record in runs]
        if [_json_text(row[0]) for row in stored] != expected:
            raise ValueError("ops.schedule_runs verification failed")
        reports.append(
            TableReport(
                table="ops.schedule_runs",
                rows=len(runs),
                sha256=_digest(
                    ("record",),
                    ("record",),
                    [(_json_text(record),) for record in runs],
                ),
                verified=True,
            )
        )
    return reports


def import_state(
    state: Path,
    database: Database | None,
    *,
    channel_root: Path | None = None,
    dry_run: bool = False,
    replace: bool = False,
) -> ImportReport:
    """Import a stopped host's legacy state; never modifies the source files."""
    root = state.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"State root is not a directory: {root}")
    if dry_run and database is not None:
        raise ValueError("dry-run does not use a database")
    if not dry_run and database is None:
        raise ValueError("a target database is required")
    skipped: list[str] = []
    plans: list[_Plan] = []
    connections: _Plan | None = None
    credentials = 0
    created: list[str] = []
    vault = None if dry_run else CredentialVault(channel_root or root / "channels")
    separate_chat = root / "runtime" / "sessions-chat" / "sessions.sqlite3"
    if separate_chat.exists() or separate_chat.is_symlink():
        # A prior release kept chat sessions in a second store. Never choose
        # between two histories the operator may need to merge themselves.
        raise ValueError(
            f"Separate legacy chat store found: {separate_chat}. "
            "Resolve or archive it before importing."
        )

    try:
        knowledge_path = root / "runtime" / "knowledge" / "knowledge.sqlite3"
        if not knowledge_path.is_file():
            knowledge_path = root / "knowledge" / "knowledge.sqlite3"
        sources = {
            "domain": root / "runtime" / "domain.sqlite3",
            "sessions": root / "runtime" / "sessions" / "sessions.sqlite3",
            "knowledge": knowledge_path,
            "channel": root / "channels" / "channels.sqlite3",
        }
        for component, path in sources.items():
            if not path.is_file():
                if path.exists():
                    raise ValueError(f"Import refuses a non-file: {path}")
                skipped.append(str(path))
                continue
            source = _open_source(path, _COMPONENT_VERSION[component])
            try:
                for table in _TABLES:
                    if table.component == component:
                        plans.append(_prepare(source, table))
                if component == "channel":
                    connections, credentials = _connection_plan(source, vault, created)
            finally:
                source.close()
        documents, runs, notes = _operations(root, skipped)

        if dry_run:
            tables = [
                TableReport(
                    table=plan.table.identity,
                    rows=len(plan.rows),
                    sha256=plan.digest,
                )
                for plan in _plans(plans, connections)
            ]
            return ImportReport(
                dry_run=True,
                replaced=False,
                credentials=credentials,
                skipped_files=tuple(skipped),
                notes=tuple(notes),
                tables=tuple(tables),
            )

        assert database is not None
        database.migrate()
        schemas = {
            domain: database.schema(domain)
            for domain in ("core", "agent", "knowledge", "channel", "ops")
        }
        all_plans = _plans(plans, connections)
        with psycopg.connect(database.url, autocommit=True) as raw:
            try:
                raw.execute("BEGIN")
                occupied = _occupied(raw, all_plans, schemas)
                if _ops_occupied(raw, schemas["ops"]):
                    occupied.append("ops.schedule_state/ops.schedule_runs")
                if occupied and not replace:
                    raise ValueError(
                        "Target already contains rows (use --replace to overwrite): "
                        + ", ".join(occupied)
                    )
                if replace:
                    _replace(raw, all_plans, schemas)
                    _ops_replace(raw, schemas["ops"])
                for plan in all_plans:
                    _insert(raw, plan, schemas)
                reports = [_verify(raw, plan, schemas) for plan in all_plans]
                reports.extend(_write_documents(raw, schemas["ops"], documents, runs))
                raw.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(Exception):
                    raw.execute("ROLLBACK")
                raise
        return ImportReport(
            dry_run=False,
            replaced=replace,
            credentials=credentials,
            skipped_files=tuple(skipped),
            notes=tuple(notes),
            tables=tuple(reports),
        )
    except BaseException:
        if vault is not None:
            for ref in created:
                vault.delete(ref)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cairn-pg-import",
        description="Import a stopped host's SQLite state into PostgreSQL.",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=Path.home() / ".local" / "state" / "cairn",
        help="Cairn data root (default: ~/.local/state/cairn)",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Target PostgreSQL URL (default: $CAIRN_DATABASE_URL or postgresql:///cairn)",
    )
    parser.add_argument(
        "--namespace",
        default=None,
        help="Target schema namespace (default: $CAIRN_DATABASE_NAMESPACE or cairn)",
    )
    parser.add_argument("--channel-root", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--replace", action="store_true")
    arguments = parser.parse_args(argv)
    database = None
    if not arguments.dry_run:
        database = Database(
            arguments.database_url
            or os.environ.get("CAIRN_DATABASE_URL", "postgresql:///cairn"),
            namespace=arguments.namespace
            or os.environ.get("CAIRN_DATABASE_NAMESPACE", "cairn"),
        )
    try:
        report = import_state(
            arguments.state,
            database,
            channel_root=arguments.channel_root,
            dry_run=arguments.dry_run,
            replace=arguments.replace,
        )
    finally:
        if database is not None:
            database.close()
    print(report.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
