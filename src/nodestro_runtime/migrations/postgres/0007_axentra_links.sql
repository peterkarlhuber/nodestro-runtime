-- Links between Nodestro's records and the Axentra platform services (server profile, NODESTRO_AXENTRA=on).
-- Nodestro ids stay unchanged everywhere; this table maps them to Axentra's ids (ten_..., usr_..., ses_..., key_...).
-- Additive only: without Axentra the table stays empty and nothing reads it.
CREATE TABLE IF NOT EXISTS nodestro_axentra_ids (
    kind text NOT NULL CHECK (kind IN ('tenant', 'user', 'session', 'api_key')),
    nodestro_id text COLLATE "C" NOT NULL CHECK (nodestro_id <> ''),
    axentra_id text COLLATE "C" NOT NULL CHECK (axentra_id <> ''),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (kind, nodestro_id),
    UNIQUE (kind, axentra_id)
);
