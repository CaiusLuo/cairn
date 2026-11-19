-- Cairn PostgreSQL schema. __CORE__, __AGENT__, __KNOWLEDGE__, __CHANNEL__
-- and __OPS__ are replaced with the configured namespaced schema names.

CREATE SCHEMA IF NOT EXISTS __CORE__;
CREATE SCHEMA IF NOT EXISTS __AGENT__;
CREATE SCHEMA IF NOT EXISTS __KNOWLEDGE__;
CREATE SCHEMA IF NOT EXISTS __CHANNEL__;
CREATE SCHEMA IF NOT EXISTS __OPS__;

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------------
-- core: conversations, tasks, runs, handoffs
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS __CORE__.conversations (
    conversation_id text PRIMARY KEY,
    workspace_id text NOT NULL,
    source text NOT NULL,
    initial_prompt text NOT NULL,
    title text,
    created_at text NOT NULL,
    updated_at text NOT NULL,
    archived_at text,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS conversations_workspace_idx
    ON __CORE__.conversations(workspace_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS __CORE__.wechat_bindings (
    workspace_id text NOT NULL,
    account_digest text NOT NULL,
    sender_digest text NOT NULL,
    conversation_id text NOT NULL REFERENCES __CORE__.conversations(conversation_id),
    created_at text NOT NULL,
    updated_at text NOT NULL,
    PRIMARY KEY (workspace_id, account_digest, sender_digest)
);

CREATE TABLE IF NOT EXISTS __CORE__.channel_bindings (
    workspace_id text NOT NULL,
    connection_id text NOT NULL,
    platform text NOT NULL,
    binding_digest text NOT NULL,
    conversation_id text NOT NULL REFERENCES __CORE__.conversations(conversation_id),
    created_at text NOT NULL,
    updated_at text NOT NULL,
    PRIMARY KEY (workspace_id, connection_id, binding_digest)
);

CREATE TABLE IF NOT EXISTS __CORE__.tasks (
    task_id text PRIMARY KEY,
    conversation_id text NOT NULL REFERENCES __CORE__.conversations(conversation_id),
    workspace_id text NOT NULL,
    title text NOT NULL,
    goal text NOT NULL,
    status text NOT NULL,
    issue_url text,
    worktree text,
    branch text,
    created_at text NOT NULL,
    updated_at text NOT NULL,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS tasks_conversation_idx
    ON __CORE__.tasks(conversation_id);

CREATE TABLE IF NOT EXISTS __CORE__.runs (
    run_id text PRIMARY KEY,
    conversation_id text NOT NULL REFERENCES __CORE__.conversations(conversation_id),
    task_id text REFERENCES __CORE__.tasks(task_id),
    workspace_id text NOT NULL,
    status text NOT NULL,
    error text,
    budget_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    trace_ref text,
    session_id text,
    checkpoint_receipt_json jsonb,
    created_at text NOT NULL,
    updated_at text NOT NULL,
    ended_at text,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS runs_conversation_idx
    ON __CORE__.runs(conversation_id);
CREATE INDEX IF NOT EXISTS runs_task_idx ON __CORE__.runs(task_id);
CREATE INDEX IF NOT EXISTS runs_active_idx
    ON __CORE__.runs(conversation_id)
    WHERE status IN ('queued', 'running');

CREATE TABLE IF NOT EXISTS __CORE__.run_events (
    id bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    run_id text NOT NULL REFERENCES __CORE__.runs(run_id),
    sequence bigint NOT NULL,
    kind text NOT NULL,
    data_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at text NOT NULL,
    PRIMARY KEY (run_id, sequence)
);
CREATE INDEX IF NOT EXISTS run_events_prune_idx
    ON __CORE__.run_events(kind, created_at);

CREATE TABLE IF NOT EXISTS __CORE__.conversation_requests (
    workspace_id text NOT NULL,
    request_id text NOT NULL,
    conversation_id text NOT NULL REFERENCES __CORE__.conversations(conversation_id),
    fingerprint text NOT NULL,
    PRIMARY KEY (workspace_id, request_id)
);

CREATE TABLE IF NOT EXISTS __CORE__.run_requests (
    conversation_id text NOT NULL REFERENCES __CORE__.conversations(conversation_id),
    request_id text NOT NULL,
    run_id text NOT NULL REFERENCES __CORE__.runs(run_id),
    fingerprint text NOT NULL,
    PRIMARY KEY (conversation_id, request_id)
);

CREATE TABLE IF NOT EXISTS __CORE__.development_handoffs (
    run_id text PRIMARY KEY REFERENCES __CORE__.runs(run_id),
    conversation_id text NOT NULL REFERENCES __CORE__.conversations(conversation_id),
    request_id text NOT NULL UNIQUE,
    status text NOT NULL,
    runtime_task_id text,
    domain_task_id text,
    prompt text NOT NULL,
    source text NOT NULL,
    diagnostic text,
    history_origins_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at text NOT NULL,
    updated_at text NOT NULL,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS development_handoffs_status_idx
    ON __CORE__.development_handoffs(status);

CREATE TABLE IF NOT EXISTS __CORE__.handoff_reviews (
    request_id text PRIMARY KEY,
    run_id text NOT NULL REFERENCES __CORE__.development_handoffs(run_id),
    action text NOT NULL,
    expected_updated_at text NOT NULL,
    note text NOT NULL,
    status text NOT NULL,
    result_status text,
    diagnostic text,
    created_at text NOT NULL,
    updated_at text NOT NULL
);
CREATE INDEX IF NOT EXISTS handoff_reviews_run_idx
    ON __CORE__.handoff_reviews(run_id);

CREATE TABLE IF NOT EXISTS __CORE__.domain_meta (
    key text PRIMARY KEY,
    value text NOT NULL
);

-- ---------------------------------------------------------------------------
-- agent: LangGraph session ledger
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS __AGENT__.sessions (
    session_id text PRIMARY KEY,
    version bigint NOT NULL CHECK (version >= 0),
    head_seq bigint NOT NULL CHECK (head_seq >= 0),
    state text NOT NULL CHECK (state IN ('ready','executing','recovery_required')),
    active_turn_id text,
    binding_json text NOT NULL,
    created_at_utc text NOT NULL,
    updated_at_utc text NOT NULL,
    CHECK ((state = 'ready' AND active_turn_id IS NULL)
        OR (state = 'executing' AND active_turn_id IS NOT NULL)
        OR state = 'recovery_required')
);

CREATE TABLE IF NOT EXISTS __AGENT__.turns (
    seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    session_id text NOT NULL REFERENCES __AGENT__.sessions(session_id),
    turn_id text NOT NULL,
    base_seq bigint NOT NULL,
    task_id text,
    state text NOT NULL CHECK (state IN
        ('executing','completed','interrupted','recovery_required','abandoned')),
    input_digest text NOT NULL,
    started_at_utc text NOT NULL,
    ended_at_utc text,
    settled_seq bigint,
    diagnostics_json text NOT NULL,
    PRIMARY KEY (session_id, turn_id),
    CHECK ((state IN ('completed','interrupted') AND settled_seq IS NOT NULL)
        OR (state IN ('executing','recovery_required','abandoned')
            AND settled_seq IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS one_unresolved_turn ON __AGENT__.turns(session_id)
    WHERE state IN ('executing','recovery_required');
CREATE INDEX IF NOT EXISTS session_turn_order ON __AGENT__.turns(session_id, seq);

CREATE TABLE IF NOT EXISTS __AGENT__.checkpoints (
    session_id text NOT NULL REFERENCES __AGENT__.sessions(session_id),
    seq bigint NOT NULL CHECK (seq >= 0),
    turn_id text,
    format_version integer NOT NULL CHECK (format_version IN (1, 2)),
    created_at_utc text NOT NULL,
    payload bytea NOT NULL CHECK (octet_length(payload) <= 8388608),
    sha256 text NOT NULL CHECK (length(sha256) = 64),
    PRIMARY KEY (session_id, seq),
    UNIQUE (session_id, turn_id),
    CHECK ((seq = 0 AND turn_id IS NULL) OR (seq > 0 AND turn_id IS NOT NULL)),
    FOREIGN KEY (session_id, turn_id)
        REFERENCES __AGENT__.turns(session_id, turn_id)
        DEFERRABLE INITIALLY DEFERRED
);

ALTER TABLE __AGENT__.sessions
    DROP CONSTRAINT IF EXISTS sessions_head_checkpoint_fk;
ALTER TABLE __AGENT__.sessions
    ADD CONSTRAINT sessions_head_checkpoint_fk
    FOREIGN KEY (session_id, head_seq)
    REFERENCES __AGENT__.checkpoints(session_id, seq)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE __AGENT__.sessions
    DROP CONSTRAINT IF EXISTS sessions_active_turn_fk;
ALTER TABLE __AGENT__.sessions
    ADD CONSTRAINT sessions_active_turn_fk
    FOREIGN KEY (session_id, active_turn_id)
    REFERENCES __AGENT__.turns(session_id, turn_id)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE __AGENT__.turns
    DROP CONSTRAINT IF EXISTS turns_base_checkpoint_fk;
ALTER TABLE __AGENT__.turns
    ADD CONSTRAINT turns_base_checkpoint_fk
    FOREIGN KEY (session_id, base_seq)
    REFERENCES __AGENT__.checkpoints(session_id, seq);
ALTER TABLE __AGENT__.turns
    DROP CONSTRAINT IF EXISTS turns_settled_checkpoint_fk;
ALTER TABLE __AGENT__.turns
    ADD CONSTRAINT turns_settled_checkpoint_fk
    FOREIGN KEY (session_id, settled_seq)
    REFERENCES __AGENT__.checkpoints(session_id, seq)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE IF NOT EXISTS __AGENT__.tool_fences (
    session_id text NOT NULL,
    turn_id text NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal BETWEEN 1 AND 256),
    assistant_message_index integer NOT NULL
        CHECK (assistant_message_index >= 0 AND assistant_message_index < 10000),
    call_id text NOT NULL
        CHECK (octet_length(call_id) BETWEEN 1 AND 1024),
    tool_name text NOT NULL
        CHECK (octet_length(tool_name) BETWEEN 1 AND 1024),
    observed_at_utc text NOT NULL,
    PRIMARY KEY (session_id, turn_id, ordinal),
    UNIQUE (session_id, turn_id, assistant_message_index, call_id),
    FOREIGN KEY (session_id, turn_id)
        REFERENCES __AGENT__.turns(session_id, turn_id)
);

CREATE TABLE IF NOT EXISTS __AGENT__.tasks (
    session_id text NOT NULL REFERENCES __AGENT__.sessions(session_id),
    task_id text NOT NULL,
    version bigint NOT NULL CHECK (version >= 0),
    phase text NOT NULL CHECK (phase IN
        ('running','turn_committed','inspecting','result_recorded')),
    base_seq bigint NOT NULL CHECK (base_seq >= 0),
    checkpoint_seq bigint,
    record_json text NOT NULL CHECK (octet_length(record_json) <= 4096),
    PRIMARY KEY (session_id, task_id),
    CHECK ((phase = 'running' AND checkpoint_seq IS NULL)
        OR (phase != 'running' AND checkpoint_seq IS NOT NULL
            AND checkpoint_seq = base_seq + 1)),
    FOREIGN KEY (session_id, base_seq)
        REFERENCES __AGENT__.checkpoints(session_id, seq),
    FOREIGN KEY (session_id, checkpoint_seq)
        REFERENCES __AGENT__.checkpoints(session_id, seq)
);

-- ---------------------------------------------------------------------------
-- knowledge: clips, reviews, documents and retrieval
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS __KNOWLEDGE__.clips (
    clip_id text PRIMARY KEY,
    content text NOT NULL,
    source text NOT NULL,
    tags_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    content_hash text NOT NULL,
    created_at text NOT NULL,
    updated_at text NOT NULL,
    archived_at text
);
CREATE INDEX IF NOT EXISTS clips_updated_idx
    ON __KNOWLEDGE__.clips(updated_at DESC, clip_id DESC);
CREATE TABLE IF NOT EXISTS __KNOWLEDGE__.clip_requests (
    request_id text PRIMARY KEY,
    fingerprint text NOT NULL,
    clip_id text NOT NULL REFERENCES __KNOWLEDGE__.clips(clip_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS __KNOWLEDGE__.reviews (
    review_id text PRIMARY KEY,
    review_type text NOT NULL,
    review_date text NOT NULL,
    period_start text NOT NULL,
    period_end text NOT NULL,
    timezone text NOT NULL,
    status text NOT NULL,
    source_manifest_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    collection_status_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    source_snapshots_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    input_fingerprint text NOT NULL,
    summary_json jsonb,
    summary_markdown text,
    model_id text,
    generation_attempts integer NOT NULL DEFAULT 0,
    created_at text NOT NULL,
    updated_at text NOT NULL,
    completed_at text,
    error text,
    version integer NOT NULL DEFAULT 1,
    supersedes_review_id text REFERENCES __KNOWLEDGE__.reviews(review_id)
);
CREATE INDEX IF NOT EXISTS reviews_period_idx
    ON __KNOWLEDGE__.reviews(review_type, review_date, timezone, created_at DESC);
CREATE INDEX IF NOT EXISTS reviews_created_idx
    ON __KNOWLEDGE__.reviews(created_at DESC, review_id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS reviews_active_period_idx
    ON __KNOWLEDGE__.reviews(review_type, period_start, period_end, timezone)
    WHERE status IN ('pending', 'generating');
CREATE TABLE IF NOT EXISTS __KNOWLEDGE__.review_requests (
    request_id text PRIMARY KEY,
    fingerprint text NOT NULL,
    review_id text NOT NULL REFERENCES __KNOWLEDGE__.reviews(review_id)
);
CREATE TABLE IF NOT EXISTS __KNOWLEDGE__.review_deliveries (
    delivery_id text PRIMARY KEY,
    review_id text NOT NULL UNIQUE REFERENCES __KNOWLEDGE__.reviews(review_id)
        ON DELETE CASCADE,
    review_version integer NOT NULL,
    channel text NOT NULL,
    account_id text NOT NULL DEFAULT '',
    recipient text NOT NULL DEFAULT '',
    status text NOT NULL,
    attempt_id text NOT NULL DEFAULT '',
    attempts integer NOT NULL DEFAULT 0,
    context_token text NOT NULL DEFAULT '',
    last_error text,
    created_at text NOT NULL,
    attempted_at text,
    completed_at text,
    updated_at text NOT NULL
);
CREATE INDEX IF NOT EXISTS review_deliveries_status_idx
    ON __KNOWLEDGE__.review_deliveries(status, updated_at);
CREATE TABLE IF NOT EXISTS __KNOWLEDGE__.review_delivery_requests (
    request_id text PRIMARY KEY,
    review_id text NOT NULL REFERENCES __KNOWLEDGE__.reviews(review_id)
        ON DELETE CASCADE,
    delivery_id text NOT NULL DEFAULT '',
    action text NOT NULL,
    rebind integer NOT NULL DEFAULT 0,
    outcome text NOT NULL,
    error text,
    created_at text NOT NULL
);
CREATE INDEX IF NOT EXISTS review_delivery_requests_review_idx
    ON __KNOWLEDGE__.review_delivery_requests(review_id, created_at);

-- Document ingestion status and graph tables come from
-- migrations/0002_knowledge_rag.sql, which upgrades existing databases.
CREATE TABLE IF NOT EXISTS __KNOWLEDGE__.documents (
    document_id text PRIMARY KEY,
    owner text NOT NULL,
    kind text NOT NULL,
    title text NOT NULL,
    media_type text NOT NULL,
    sha256 text NOT NULL,
    size_bytes bigint NOT NULL,
    source_url text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (owner, sha256)
);
CREATE TABLE IF NOT EXISTS __KNOWLEDGE__.document_texts (
    document_id text PRIMARY KEY
        REFERENCES __KNOWLEDGE__.documents(document_id) ON DELETE CASCADE,
    text text NOT NULL,
    parser text NOT NULL,
    parser_version text NOT NULL,
    extracted_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS __KNOWLEDGE__.chunks (
    chunk_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id text NOT NULL
        REFERENCES __KNOWLEDGE__.documents(document_id) ON DELETE CASCADE,
    ordinal integer NOT NULL,
    text text NOT NULL,
    token_count integer NOT NULL,
    heading_path text[],
    locator jsonb NOT NULL DEFAULT '{}'::jsonb,
    search tsvector GENERATED ALWAYS AS
        (to_tsvector('simple'::regconfig, text)) STORED,
    embedding vector,
    embedding_model text,
    embedding_sha text,
    embedded_at timestamptz,
    UNIQUE (document_id, ordinal)
);
CREATE INDEX IF NOT EXISTS chunks_search_idx ON __KNOWLEDGE__.chunks USING gin (search);
CREATE INDEX IF NOT EXISTS chunks_trgm_idx
    ON __KNOWLEDGE__.chunks USING gin (text gin_trgm_ops);
CREATE TABLE IF NOT EXISTS __KNOWLEDGE__.embedding_spaces (
    name text PRIMARY KEY,
    model text NOT NULL,
    dims integer NOT NULL CHECK (dims > 0),
    metric text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- channel: connections, inbound/outbound audit
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS __CHANNEL__.channel_connections (
    connection_id text PRIMARY KEY,
    platform text NOT NULL,
    display_name text NOT NULL,
    enabled integer NOT NULL,
    desired_state text NOT NULL,
    credential_ref text,
    credential_created_at text,
    owner_identity text,
    allowed_senders_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    revision bigint NOT NULL,
    created_at text NOT NULL,
    updated_at text NOT NULL
);
CREATE TABLE IF NOT EXISTS __CHANNEL__.channel_requests (
    request_id text PRIMARY KEY,
    operation text NOT NULL,
    digest text NOT NULL,
    state text NOT NULL,
    result_json jsonb,
    created_at text NOT NULL,
    updated_at text NOT NULL
);
CREATE TABLE IF NOT EXISTS __CHANNEL__.channel_inbound (
    platform text NOT NULL,
    connection_id text NOT NULL,
    external_message_id text NOT NULL,
    event_digest text NOT NULL,
    state text NOT NULL,
    conversation_id text,
    run_id text,
    conversation_binding text NOT NULL DEFAULT '',
    sender_id text NOT NULL DEFAULT '',
    sender_role text NOT NULL DEFAULT '',
    adapter_identity text NOT NULL DEFAULT '',
    request_id text NOT NULL DEFAULT '',
    error text,
    received_at text NOT NULL,
    updated_at text NOT NULL,
    PRIMARY KEY (connection_id, adapter_identity, external_message_id)
);
CREATE TABLE IF NOT EXISTS __CHANNEL__.channel_inbound_inbox (
    seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    connection_id text NOT NULL,
    adapter_identity text NOT NULL,
    external_message_id text NOT NULL,
    platform text NOT NULL,
    event_digest text NOT NULL,
    sender_id text NOT NULL,
    conversation_binding text NOT NULL,
    text text NOT NULL,
    received_at text NOT NULL,
    state text NOT NULL,
    attempts integer NOT NULL DEFAULT 0,
    last_error text,
    next_attempt_at double precision,
    deadline_at double precision NOT NULL,
    created_at text NOT NULL,
    updated_at text NOT NULL,
    PRIMARY KEY (connection_id, adapter_identity, external_message_id)
);
CREATE INDEX IF NOT EXISTS idx_channel_inbound_inbox_due
    ON __CHANNEL__.channel_inbound_inbox
    (connection_id, adapter_identity, state, next_attempt_at);
CREATE TABLE IF NOT EXISTS __CHANNEL__.channel_deliveries (
    delivery_id text PRIMARY KEY,
    connection_id text NOT NULL,
    platform text NOT NULL,
    idempotency_key text NOT NULL,
    target_digest text NOT NULL,
    status text NOT NULL,
    error text,
    created_at text NOT NULL,
    updated_at text NOT NULL,
    UNIQUE (connection_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS __CHANNEL__.channel_replies (
    reply_id text PRIMARY KEY,
    connection_id text NOT NULL,
    conversation_id text NOT NULL,
    run_id text NOT NULL UNIQUE,
    target text NOT NULL,
    external_message_id text NOT NULL,
    sender_id text NOT NULL DEFAULT '',
    sender_role text NOT NULL DEFAULT '',
    adapter_identity text NOT NULL DEFAULT '',
    state text NOT NULL,
    attempts integer NOT NULL DEFAULT 0,
    error text,
    created_at text NOT NULL,
    updated_at text NOT NULL
);
CREATE TABLE IF NOT EXISTS __CHANNEL__.channel_adapter_state (
    connection_id text NOT NULL,
    key text NOT NULL,
    value_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at text NOT NULL,
    PRIMARY KEY (connection_id, key)
);

-- ---------------------------------------------------------------------------
-- ops: schedule document and runtime catalogue
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS __OPS__.schedule_state (
    key text PRIMARY KEY,
    payload jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS __OPS__.schedule_runs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    record jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS __OPS__.runtime_state (
    key text PRIMARY KEY,
    payload jsonb NOT NULL
);
