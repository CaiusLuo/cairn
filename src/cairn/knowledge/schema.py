"""One SQLite schema shared by Knowledge Clips and Review reports.

The knowledge database deliberately keeps both stores in the same file: a
review is derived from clips and its source manifest must refer to the same
durable identities.  Schema upgrades are additive so a K1 database remains
readable without exporting or rebuilding it.
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    clip_id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    source TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT
);
CREATE INDEX IF NOT EXISTS clips_updated_idx
    ON clips(updated_at DESC, clip_id DESC);
CREATE TABLE IF NOT EXISTS clip_requests (
    request_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    clip_id TEXT NOT NULL REFERENCES clips(clip_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS reviews (
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
CREATE INDEX IF NOT EXISTS reviews_period_idx
    ON reviews(review_type, review_date, timezone, created_at DESC);
CREATE INDEX IF NOT EXISTS reviews_created_idx
    ON reviews(created_at DESC, review_id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS reviews_active_period_idx
    ON reviews(review_type, period_start, period_end, timezone)
    WHERE status IN ('pending', 'generating');
CREATE TABLE IF NOT EXISTS review_requests (
    request_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    review_id TEXT NOT NULL REFERENCES reviews(review_id)
);
"""


def create_schema(connection: sqlite3.Connection) -> None:
    """Create every table/index and leave existing rows untouched."""
    connection.executescript(SCHEMA)


__all__ = ["SCHEMA", "SCHEMA_VERSION", "create_schema"]
