-- MUP-ONA A9 prototype: removes what policies.sql added (test databases only; the benchmark toggles the policies).

DROP POLICY IF EXISTS nodestro_tenant ON nodestro_documents;
ALTER TABLE nodestro_documents NO FORCE ROW LEVEL SECURITY;
ALTER TABLE nodestro_documents DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS nodestro_tenant ON nodestro_files;
ALTER TABLE nodestro_files NO FORCE ROW LEVEL SECURITY;
ALTER TABLE nodestro_files DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS nodestro_tenant ON nodestro_chunks;
ALTER TABLE nodestro_chunks NO FORCE ROW LEVEL SECURITY;
ALTER TABLE nodestro_chunks DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS nodestro_tenant ON nodestro_memory_files;
ALTER TABLE nodestro_memory_files NO FORCE ROW LEVEL SECURITY;
ALTER TABLE nodestro_memory_files DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS nodestro_tenant ON nodestro_vector_spaces;
ALTER TABLE nodestro_vector_spaces NO FORCE ROW LEVEL SECURITY;
ALTER TABLE nodestro_vector_spaces DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS nodestro_tenant ON nodestro_chunk_vectors;
ALTER TABLE nodestro_chunk_vectors NO FORCE ROW LEVEL SECURITY;
ALTER TABLE nodestro_chunk_vectors DISABLE ROW LEVEL SECURITY;
