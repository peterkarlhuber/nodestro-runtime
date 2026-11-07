-- MUP-ONA A9 (RLS assessment): prototype policies for a TEST database only. This is not a migration, is never
-- applied by the runtime's migration runner and must never run on a shared or production database.
--
-- Scope: the knowledge and memory tables of migrations 0002 and 0010 (option (b) of the A9 report) plus
-- nodestro_documents (the table the M10 benchmark measured), so the same benchmarks can be compared.
--
-- Every row is visible and writable only when its tenant_id equals the transaction-local setting app.tenant_id
-- (set with set_config('app.tenant_id', <tenant>, true), the hook of orkexa's PsycopgSource and Nodestro's
-- DatabaseSource). current_setting(..., true) returns NULL when the setting was never defined in the session and
-- '' after a transaction that set it has ended; neither equals a tenant_id (CHECK tenant_id <> ''), so a statement
-- without the setting sees no row and can write none (fail closed). FORCE makes the owner role subject to the
-- policies too; superusers and roles with BYPASSRLS are never subject to them.

ALTER TABLE nodestro_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE nodestro_documents FORCE ROW LEVEL SECURITY;
CREATE POLICY nodestro_tenant ON nodestro_documents
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE nodestro_files ENABLE ROW LEVEL SECURITY;
ALTER TABLE nodestro_files FORCE ROW LEVEL SECURITY;
CREATE POLICY nodestro_tenant ON nodestro_files
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE nodestro_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE nodestro_chunks FORCE ROW LEVEL SECURITY;
CREATE POLICY nodestro_tenant ON nodestro_chunks
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE nodestro_memory_files ENABLE ROW LEVEL SECURITY;
ALTER TABLE nodestro_memory_files FORCE ROW LEVEL SECURITY;
CREATE POLICY nodestro_tenant ON nodestro_memory_files
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE nodestro_vector_spaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE nodestro_vector_spaces FORCE ROW LEVEL SECURITY;
CREATE POLICY nodestro_tenant ON nodestro_vector_spaces
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE nodestro_chunk_vectors ENABLE ROW LEVEL SECURITY;
ALTER TABLE nodestro_chunk_vectors FORCE ROW LEVEL SECURITY;
CREATE POLICY nodestro_tenant ON nodestro_chunk_vectors
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
