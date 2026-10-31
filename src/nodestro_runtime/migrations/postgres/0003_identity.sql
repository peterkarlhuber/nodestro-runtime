-- Identity and tenants (ADR-0003): tenants, users from OpenID Connect, roles per tenant, sessions, login states and
-- the one-time bootstrap tokens that bind a tenant's first owner. Secrets (session ids, login states, bootstrap
-- tokens) are stored only as SHA-256 hashes.

CREATE TABLE nodestro_tenants (
    id text COLLATE "C" PRIMARY KEY,
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (id <> '')
);

CREATE TABLE nodestro_users (
    id text COLLATE "C" PRIMARY KEY,
    issuer text COLLATE "C" NOT NULL,
    subject text COLLATE "C" NOT NULL,
    email text,
    name text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_login_at timestamptz,
    UNIQUE (issuer, subject)
);

CREATE TABLE nodestro_memberships (
    tenant_id text COLLATE "C" NOT NULL REFERENCES nodestro_tenants (id) ON DELETE CASCADE,
    user_id text COLLATE "C" NOT NULL REFERENCES nodestro_users (id) ON DELETE CASCADE,
    role text NOT NULL CHECK (role IN ('owner', 'admin', 'editor', 'operator', 'viewer')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, user_id)
);
CREATE INDEX nodestro_memberships_user ON nodestro_memberships (user_id);

CREATE TABLE nodestro_sessions (
    token_hash bytea PRIMARY KEY,
    user_id text COLLATE "C" NOT NULL REFERENCES nodestro_users (id) ON DELETE CASCADE,
    tenant_id text COLLATE "C" NOT NULL REFERENCES nodestro_tenants (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL
);
CREATE INDEX nodestro_sessions_user ON nodestro_sessions (user_id);
CREATE INDEX nodestro_sessions_expiry ON nodestro_sessions (expires_at);

CREATE TABLE nodestro_login_states (
    state_hash bytea PRIMARY KEY,
    nonce text NOT NULL,
    code_verifier text NOT NULL,
    return_to text NOT NULL,
    bootstrap_hash bytea,
    expires_at timestamptz NOT NULL
);

CREATE TABLE nodestro_bootstrap_tokens (
    token_hash bytea PRIMARY KEY,
    tenant_id text COLLATE "C" NOT NULL REFERENCES nodestro_tenants (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    used_at timestamptz,
    used_by text COLLATE "C" REFERENCES nodestro_users (id) ON DELETE SET NULL
);
