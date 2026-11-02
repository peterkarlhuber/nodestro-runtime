-- A tenant can be suspended (server profile): its members' sessions stop working at once, no new session starts,
-- and workers neither start nor continue its runs. Suspending keeps every record; resuming restores access.
-- Additive: existing tenants are active.
ALTER TABLE nodestro_tenants
    ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended'));
