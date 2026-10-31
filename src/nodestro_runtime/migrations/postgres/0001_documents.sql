-- JSON documents of every tenant (ADR-0002, ADR-0003). Ids and collections compare by code point ("C"), as in SQLite.
-- jsonb cannot hold the character U+0000: a document with it keeps its exact JSON in raw, and data has U+FFFD in its
-- place (for queries only); reads return coalesce(raw, data::text).
CREATE TABLE nodestro_documents (
    tenant_id text COLLATE "C" NOT NULL,
    collection text COLLATE "C" NOT NULL,
    id text COLLATE "C" NOT NULL,
    data jsonb NOT NULL,
    raw text,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, collection, id),
    CHECK (tenant_id <> '')
);

-- list(): newest first per collection.
CREATE INDEX nodestro_documents_updated ON nodestro_documents (tenant_id, collection, updated_at DESC, id);

-- find("runs", "status", ...): the engine looks for queued and running runs at start-up.
CREATE INDEX nodestro_documents_status ON nodestro_documents (tenant_id, collection, (data ->> 'status'));
