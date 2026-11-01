-- find("runs", "trigger_ref", ...): the runs of a publication (limits, daily cost, waiting approvals of the API).
-- find() names the field as a literal, so this expression index (and nodestro_documents_status) applies.
CREATE INDEX nodestro_documents_trigger_ref ON nodestro_documents (tenant_id, collection, (data ->> 'trigger_ref'));
