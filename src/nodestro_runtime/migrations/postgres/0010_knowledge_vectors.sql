-- Semantic knowledge search (local models, D10): the vectors of knowledge chunks, per tenant, namespace and
-- embedding model. A "space" is the vectors of one embedding model identity (model, digest, dimensions,
-- instructions; orkexa.embeddings.EmbeddingIdentity). Vectors of different spaces are never compared. A reindex
-- fills a 'building' space and switches it to 'active' in one transaction (PostgresIndex.reindex).

CREATE TABLE nodestro_vector_spaces (
    tenant_id text COLLATE "C" NOT NULL,
    namespace text COLLATE "C" NOT NULL,
    space text COLLATE "C" NOT NULL,
    identity jsonb NOT NULL,
    dimensions integer NOT NULL CHECK (dimensions > 0),
    state text COLLATE "C" NOT NULL CHECK (state IN ('active', 'building')),
    PRIMARY KEY (tenant_id, namespace, space),
    CHECK (tenant_id <> '')
);

-- The vectors themselves as real[]: ranked with pgvector (a cast to vector) where the extension is installed, else
-- by the runtime. No approximate index: searches are exact, and scoped to the allowed sources before ranking.
CREATE TABLE nodestro_chunk_vectors (
    tenant_id text COLLATE "C" NOT NULL,
    namespace text COLLATE "C" NOT NULL,
    space text COLLATE "C" NOT NULL,
    chunk_id bigint NOT NULL REFERENCES nodestro_chunks (id) ON DELETE CASCADE,
    embedding real[] NOT NULL,
    PRIMARY KEY (tenant_id, space, chunk_id),
    FOREIGN KEY (tenant_id, namespace, space) REFERENCES nodestro_vector_spaces (tenant_id, namespace, space)
        ON DELETE CASCADE,
    CHECK (tenant_id <> '')
);
CREATE INDEX nodestro_chunk_vectors_chunk ON nodestro_chunk_vectors (chunk_id);

-- pgvector, where the server's PostgreSQL has it and the migrating role may install it (the pinned server image
-- has it). Without it, semantic search still works (ranked by the runtime) and the server's status says so.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')
        AND EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector') THEN
        BEGIN
            CREATE EXTENSION vector;
        EXCEPTION WHEN others THEN
            RAISE NOTICE 'pgvector could not be installed (%): vectors are ranked by the runtime', SQLERRM;
        END;
    END IF;
END
$$;
