-- The find() indexes (status from 0001, trigger_ref from 0005) start with (tenant_id, collection) like the primary
-- key and covered every document. Without fresh statistics the planner could pick one of them for a lookup by id
-- (the estimates tie) and scan the whole collection. Partial indexes only apply to queries that compare the field,
-- which find() does (a strict comparison implies IS NOT NULL), so a lookup by id uses the primary key.
DROP INDEX IF EXISTS nodestro_documents_status;
CREATE INDEX nodestro_documents_status ON nodestro_documents (tenant_id, collection, (data ->> 'status'))
    WHERE (data ->> 'status') IS NOT NULL;
DROP INDEX IF EXISTS nodestro_documents_trigger_ref;
CREATE INDEX nodestro_documents_trigger_ref ON nodestro_documents (tenant_id, collection, (data ->> 'trigger_ref'))
    WHERE (data ->> 'trigger_ref') IS NOT NULL;
