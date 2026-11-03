-- Keyset run listing (RT2-05): PostgresStore.list_page() reads one page of runs by creation (newest first, ties by
-- id) from an index instead of reading every run of the tenant. Runs keep their creation time in the document
-- (data ->> 'created_at', ISO 8601 text), and a cast of text to timestamptz may not appear in an index (it depends on
-- the session's TimeZone and DateStyle, and accepts words such as 'now'). nodestro_created_order() fixes both
-- settings and only casts text that starts with a date and a time, so it is truly immutable; any other value
-- (missing, damaged) gives NULL instead of an error, so a strange document never stops a write.
CREATE OR REPLACE FUNCTION nodestro_created_order(value text) RETURNS timestamptz
    LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
    SET "TimeZone" = 'UTC'
    SET datestyle = 'ISO, YMD'
    SET search_path = pg_catalog
AS $$
BEGIN
    IF value IS NULL OR value !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}' THEN
        RETURN NULL;
    END IF;
    RETURN value::timestamptz;
EXCEPTION WHEN others THEN
    RETURN NULL;
END
$$;

-- All runs, and the top-level runs alone (sub-workflow and team runs can outnumber them by far). Partial indexes:
-- only documents of the collection runs are indexed, so writes of other collections do not pay for them.
CREATE INDEX nodestro_documents_runs_created ON nodestro_documents
    (tenant_id, collection, nodestro_created_order(data ->> 'created_at') DESC, id DESC)
    WHERE collection = 'runs';
CREATE INDEX nodestro_documents_runs_created_top ON nodestro_documents
    (tenant_id, collection, nodestro_created_order(data ->> 'created_at') DESC, id DESC)
    WHERE collection = 'runs' AND (data ->> 'parent_run_id') IS NULL;
