-- Knowledge RAG: document ingestion status and a lightweight graph.
-- Every statement is idempotent so this file can upgrade an existing 0001 database.

ALTER TABLE __KNOWLEDGE__.documents
    ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS error text,
    ADD COLUMN IF NOT EXISTS storage_path text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS embedding_status text NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS graph_status text NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS attempts integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS claimed_at timestamptz,
    ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS processed_at timestamptz;

CREATE INDEX IF NOT EXISTS documents_owner_created_idx
    ON __KNOWLEDGE__.documents(owner, created_at DESC);
CREATE INDEX IF NOT EXISTS documents_claim_idx
    ON __KNOWLEDGE__.documents(status, created_at);

CREATE TABLE IF NOT EXISTS __KNOWLEDGE__.entities (
    entity_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner text NOT NULL,
    name text NOT NULL,
    normalized_name text NOT NULL,
    kind text NOT NULL DEFAULT 'other',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (owner, normalized_name, kind)
);
CREATE INDEX IF NOT EXISTS entities_name_trgm_idx
    ON __KNOWLEDGE__.entities USING gin (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS entities_owner_normalized_idx
    ON __KNOWLEDGE__.entities(owner, normalized_name);

CREATE TABLE IF NOT EXISTS __KNOWLEDGE__.chunk_entities (
    chunk_id bigint NOT NULL
        REFERENCES __KNOWLEDGE__.chunks(chunk_id) ON DELETE CASCADE,
    entity_id bigint NOT NULL
        REFERENCES __KNOWLEDGE__.entities(entity_id) ON DELETE CASCADE,
    weight real NOT NULL DEFAULT 1,
    PRIMARY KEY (chunk_id, entity_id)
);
CREATE INDEX IF NOT EXISTS chunk_entities_entity_idx
    ON __KNOWLEDGE__.chunk_entities(entity_id);

CREATE TABLE IF NOT EXISTS __KNOWLEDGE__.relations (
    relation_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner text NOT NULL,
    source_entity_id bigint NOT NULL
        REFERENCES __KNOWLEDGE__.entities(entity_id) ON DELETE CASCADE,
    target_entity_id bigint NOT NULL
        REFERENCES __KNOWLEDGE__.entities(entity_id) ON DELETE CASCADE,
    relation text NOT NULL,
    document_id text NOT NULL
        REFERENCES __KNOWLEDGE__.documents(document_id) ON DELETE CASCADE,
    chunk_id bigint
        REFERENCES __KNOWLEDGE__.chunks(chunk_id) ON DELETE SET NULL,
    weight real NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, source_entity_id, target_entity_id, relation)
);
CREATE INDEX IF NOT EXISTS relations_source_idx
    ON __KNOWLEDGE__.relations(owner, source_entity_id);
CREATE INDEX IF NOT EXISTS relations_target_idx
    ON __KNOWLEDGE__.relations(owner, target_entity_id);
