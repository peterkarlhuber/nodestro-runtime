-- Knowledge files, their search index and memory spaces (ADR-0002), per tenant and Orkexa namespace.

-- Orkexa FileStore entries and bytes (SQLite: stored_files).
CREATE TABLE nodestro_files (
    tenant_id text COLLATE "C" NOT NULL,
    namespace text COLLATE "C" NOT NULL,
    id text COLLATE "C" NOT NULL,
    entry jsonb NOT NULL,
    data bytea NOT NULL,
    PRIMARY KEY (tenant_id, namespace, id),
    CHECK (tenant_id <> '')
);

-- Orkexa Index chunks (SQLite: index_chunks + index_fts). search_text holds the words of title and text, lower
-- case and without diacritics, separated by spaces (computed by the runtime, like SQLite's unicode61 tokenizer).
CREATE TABLE nodestro_chunks (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id text COLLATE "C" NOT NULL,
    namespace text COLLATE "C" NOT NULL,
    source text COLLATE "C" NOT NULL,
    title text,
    file_id text COLLATE "C",
    metadata jsonb NOT NULL,
    text text NOT NULL,
    search_text text NOT NULL,
    search tsvector GENERATED ALWAYS AS (to_tsvector('simple', search_text)) STORED,
    CHECK (tenant_id <> '')
);
CREATE INDEX nodestro_chunks_source ON nodestro_chunks (tenant_id, namespace, source);
CREATE INDEX nodestro_chunks_search ON nodestro_chunks USING gin (search);

-- Orkexa memory files (SQLite: memory_files); path is relative to /memories.
CREATE TABLE nodestro_memory_files (
    tenant_id text COLLATE "C" NOT NULL,
    namespace text COLLATE "C" NOT NULL,
    path text COLLATE "C" NOT NULL,
    content text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, namespace, path),
    CHECK (tenant_id <> '')
);
