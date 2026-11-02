"""Statements on tenant tables that are not bound to one tenant, by design, each with its reason (security review
SR-01, checked by :mod:`nodestro_runtime.tenant_sql_check` in ``tests/test_tenant_filters.py``).

A key is the module and the **whole** statement text as the check rebuilds it: whitespace collapsed, a part only known
at run time as ``{expression}``. Matching is exact; an entry that no longer matches a statement fails the test, so a
changed statement is reviewed again. Add an entry only for work that must see every tenant (worker claims, leader
housekeeping, operator commands, aggregate metrics) or that is keyed by a server-generated secret or id before the
tenant is known; say why it cannot leak one tenant's rows to another.
"""

from __future__ import annotations

#: Modules without PostgreSQL statements (the check fails if one of them uses PostgreSQL), each with its reason.
SKIP_MODULES: dict[str, str] = {
    "nodestro_runtime/storage.py": (
        "the desktop's SQLite document store: one data directory is one tenant and its table has no tenant_id column"
    ),
}

#: ``(module, normalized statement) -> reason``.
ALLOWLIST: dict[tuple[str, str], str] = {
    (
        "nodestro_runtime/axentra_identity.py",
        "(SELECT x.axentra_id COLLATE \"default\" FROM nodestro_axentra_ids x WHERE x.kind = '{kind}' AND x.nodestro_id = %s)",
    ): (
        "_linked() fragment: the Axentra id linked to one Nodestro id (kind is the literal 'tenant' or 'user', checked by _linked); names no tenant table"
    ),
    (
        "nodestro_runtime/axentra_identity.py",
        "SELECT l.nodestro_id, t.name, n.created_at, CASE WHEN t.status = 'active' AND n.status = 'active' THEN 'active' ELSE 'suspended' END AS status FROM axentra_tenants t JOIN nodestro_axentra_ids l ON l.kind = 'tenant' AND l.axentra_id = t.id JOIN nodestro_tenants n ON n.id = l.nodestro_id WHERE t.product_id = %s AND t.id = {_linked('tenant')} ORDER BY l.nodestro_id",
    ): (
        "names no tenant table: Axentra's tenant joined to its link and nodestro_tenants; {_linked('tenant')} is the scalar subquery of _linked() with the tenant id as a parameter"
    ),
    (
        "nodestro_runtime/axentra_identity.py",
        "SELECT lt.nodestro_id AS tenant_id, lu.nodestro_id AS user_id, m.role, CASE WHEN t.status = 'active' AND n.status = 'active' THEN 'active' ELSE 'suspended' END AS status FROM axentra_memberships m JOIN nodestro_axentra_ids lt ON lt.kind = 'tenant' AND lt.axentra_id = m.tenant_id JOIN nodestro_axentra_ids lu ON lu.kind = 'user' AND lu.axentra_id = m.user_id JOIN axentra_users u ON u.id = m.user_id AND u.status = 'active' JOIN axentra_tenants t ON t.id = m.tenant_id JOIN nodestro_tenants n ON n.id = lt.nodestro_id WHERE {where} ORDER BY lt.nodestro_id, lu.nodestro_id",
    ): (
        "names no tenant table: Axentra's memberships joined to the id links and nodestro_tenants; {where} is "
        "' AND '.join() of the constant conjuncts m.product_id = %s / lt.nodestro_id = %s / lu.nodestro_id = "
        "%s (the tenant and user filters of _memberships), values always as parameters"
    ),
    (
        "nodestro_runtime/axentra_identity.py",
        "SELECT s.user_id, s.tenant_id, m.role, r.permissions AS custom, t.status = 'active' AND n.status = 'active' AS active, s.last_seen_at < clock_timestamp() - interval '1 minute' AS stale FROM nodestro_sessions s JOIN nodestro_axentra_ids lu ON lu.kind = 'user' AND lu.nodestro_id = s.user_id JOIN nodestro_axentra_ids lt ON lt.kind = 'tenant' AND lt.nodestro_id = s.tenant_id JOIN axentra_memberships m ON m.tenant_id = lt.axentra_id COLLATE \"default\" AND m.user_id = %s JOIN axentra_tenants t ON t.id = lt.axentra_id COLLATE \"default\" JOIN nodestro_tenants n ON n.id = s.tenant_id LEFT JOIN axentra_roles r ON r.product_id = m.product_id AND r.key = m.role WHERE s.token_hash = %s AND lu.axentra_id = %s AND m.product_id = %s",
    ): (
        "session lookup (NODESTRO_IDENTITY_BACKEND=axentra) by the SHA-256 of the session token, before any "
        "tenant is known: the token picks the one session and its tenant; the user and product are bound as "
        "parameters"
    ),
    (
        "nodestro_runtime/axentra_identity.py",
        "UPDATE nodestro_sessions SET last_seen_at = clock_timestamp() WHERE token_hash = %s",
    ): ("touch of the one session whose token hash the caller presented (server-generated secret, unique key)"),
    (
        "nodestro_runtime/axentra_identity.py",
        "DELETE FROM nodestro_sessions WHERE expires_at <= clock_timestamp() OR last_seen_at <= clock_timestamp() - %s RETURNING token_hash",
    ): ("housekeeping of the leader: expired and idle sessions of every tenant; returns only token hashes to revoke"),
    (
        "nodestro_runtime/axentra_identity.py",
        "DELETE FROM nodestro_login_states WHERE expires_at <= clock_timestamp()",
    ): ("housekeeping of the leader: expired login states (they belong to no session yet)"),
    (
        "nodestro_runtime/axentra_identity.py",
        "SELECT (SELECT count(*) FROM nodestro_tenants n WHERE NOT EXISTS (SELECT 1 FROM nodestro_axentra_ids l JOIN axentra_tenants t ON t.id = l.axentra_id WHERE l.kind = 'tenant' AND l.nodestro_id = n.id)) AS tenants, (SELECT count(*) FROM nodestro_users n WHERE NOT EXISTS (SELECT 1 FROM nodestro_axentra_ids l JOIN axentra_users u ON u.id = l.axentra_id WHERE l.kind = 'user' AND l.nodestro_id = n.id)) AS users, (SELECT count(*) FROM nodestro_memberships) AS n_members, (SELECT count(*) FROM axentra_memberships WHERE product_id = %s) AS a_members, (SELECT count(*) FROM nodestro_sessions WHERE expires_at > now()) AS n_sessions, (SELECT count(*) FROM nodestro_sessions s JOIN axentra_sessions a ON a.token_hash = encode(s.token_hash, 'hex') AND a.revoked_at IS NULL WHERE s.expires_at > now()) AS both_sessions, (SELECT count(*) FROM axentra_roles WHERE product_id = %s AND key = ANY(%s)) AS roles",
    ): ("operator report (nodestro-server axentra reconcile): counts over every tenant, no row content"),
    (
        "nodestro_runtime/axentra_identity.py",
        "SELECT token_hash, user_id, created_at, last_seen_at, expires_at FROM nodestro_sessions WHERE expires_at > clock_timestamp() AND token_hash > %s ORDER BY token_hash LIMIT %s FOR UPDATE",
    ): (
        "operator backfill (nodestro-server axentra backfill-identity): copies every live session to Axentra, "
        "in token-hash batches; the CLI runs as the operator over all tenants by design"
    ),
    (
        "nodestro_runtime/axentra_identity.py",
        "SELECT a.id FROM axentra_sessions a WHERE a.revoked_at IS NULL AND a.expires_at > now() AND a.auth_method = %s AND a.user_agent = %s AND NOT EXISTS (SELECT 1 FROM nodestro_sessions s WHERE encode(s.token_hash, 'hex') = a.token_hash)",
    ): (
        "operator backfill: finds Axentra sessions without a Nodestro session (existence check by token hash "
        "over all tenants), to revoke them"
    ),
    ("nodestro_runtime/axentra_identity.py", "DELETE FROM nodestro_sessions WHERE token_hash = ANY(%s)"): (
        "operator backfill: deletes the sessions just found ended in the same batch, by their token hashes"
    ),
    ("nodestro_runtime/axentra_identity.py", "SELECT tenant_id, user_id, role FROM nodestro_memberships"): (
        "operator reconcile (nodestro-server axentra reconcile): compares every membership with Axentra's"
    ),
    (
        "nodestro_runtime/axentra_identity.py",
        "SELECT (SELECT count(*) FROM nodestro_sessions WHERE expires_at > now()) AS nodestro, (SELECT count(*) FROM axentra_sessions WHERE revoked_at IS NULL AND expires_at > now()) AS axentra",
    ): ("operator reconcile: session counts over every tenant, no row content"),
    (
        "nodestro_runtime/axentra_identity.py",
        "SELECT s.user_id, CASE WHEN a.id IS NULL THEN 'missing in Axentra' WHEN a.revoked_at IS NOT NULL THEN 'revoked in Axentra' WHEN l.axentra_id IS DISTINCT FROM a.user_id THEN 'another user in Axentra' ELSE 'different expiry' END AS problem FROM nodestro_sessions s LEFT JOIN axentra_sessions a ON a.token_hash = encode(s.token_hash, 'hex') LEFT JOIN nodestro_axentra_ids l ON l.kind = 'user' AND l.nodestro_id = s.user_id WHERE s.expires_at > now() AND (a.id IS NULL OR a.revoked_at IS NOT NULL OR l.axentra_id IS DISTINCT FROM a.user_id OR abs(extract(epoch FROM a.expires_at - s.expires_at)) > 1)",
    ): ("operator reconcile: lists sessions that differ from Axentra's, with user ids only, over every tenant"),
    (
        "nodestro_runtime/axentra_identity.py",
        "SELECT coalesce(l.nodestro_id, a.user_id) AS user_id FROM axentra_sessions a LEFT JOIN nodestro_axentra_ids l ON l.kind = 'user' AND l.axentra_id = a.user_id WHERE a.revoked_at IS NULL AND a.expires_at > now() AND NOT EXISTS (SELECT 1 FROM nodestro_sessions s WHERE encode(s.token_hash, 'hex') = a.token_hash)",
    ): ("operator reconcile: Axentra sessions without a Nodestro session (existence check by token hash)"),
    ("nodestro_runtime/axentra_metrics.py", "SELECT kind, status, count(*) FROM nodestro_jobs GROUP BY kind, status"): (
        "/api/metrics: job counts by kind and state over every tenant; no tenant, user or run labels"
    ),
    (
        "nodestro_runtime/axentra_metrics.py",
        "SELECT count(*) FILTER (WHERE lease_until < clock_timestamp()), count(DISTINCT worker) FILTER (WHERE lease_until >= clock_timestamp()), coalesce(extract(epoch FROM clock_timestamp() - min(created_at) FILTER (WHERE status = 'queued')), 0) FROM nodestro_jobs WHERE status IN ('queued', 'running')",
    ): ("/api/metrics: lease and queue-age aggregates over every tenant; no labels"),
    (
        "nodestro_runtime/axentra_metrics.py",
        "SELECT data ->> 'status', count(*), coalesce(sum(coalesce((data -> 'usage' ->> 'known_cost_usd')::numeric, (data -> 'usage' ->> 'estimated_cost_usd')::numeric, 0)), 0) FROM nodestro_documents WHERE collection = 'runs' AND data ->> 'status' IN ('succeeded', 'failed', 'cancelled') AND data ->> 'parent_run_id' IS NULL AND data ->> 'imported_at' IS NULL AND updated_at >= now() - interval '24 hours' AND (data ->> 'finished_at')::timestamptz >= now() - interval '24 hours' GROUP BY 1",
    ): ("/api/metrics: run counts and cost by state over every tenant in the last 24 h; no labels"),
    ("nodestro_runtime/axentra_metrics.py", "nodestro_jobs"): (
        "not SQL: the name of the Prometheus gauge nodestro_jobs"
    ),
    (
        "nodestro_runtime/axentra_platform.py",
        "SELECT tenant_id, collection, id FROM nodestro_documents WHERE collection = ANY(%s) ORDER BY tenant_id, collection, id",
    ): (
        "secrets reencrypt (operator CLI) without --tenant: lists the secret documents of every tenant by key "
        "only (tenant, collection, id); each one is then read and re-encrypted under its own tenant_id"
    ),
    (
        "nodestro_runtime/axentra_platform.py",
        "SELECT collection, id, coalesce(raw, data::text) FROM nodestro_documents WHERE collection = ANY(%s) AND (strpos(data::text, %s) > 0 OR strpos(data::text, %s) > 0)",
    ): (
        "start check (stored_secret_formats): counts stored secrets by format and key id over every tenant; "
        "decrypts nothing and returns counts only"
    ),
    (
        "nodestro_runtime/axentra_platform.py",
        "SELECT tenant_id, collection, id, coalesce(raw, data::text) FROM nodestro_documents WHERE collection = ANY(%s) AND (strpos(data::text, %s) > 0 OR strpos(data::text, %s) > 0) ORDER BY tenant_id, collection, id",
    ): (
        "start check (secret_key_problems, SR-04): decrypts up to five stored secrets per key, each under its own "
        "tenant_id, to prove the configured key bytes; returns problems with locations only, never values"
    ),
    (
        "nodestro_runtime/axentra_platform.py",
        "SELECT count(*) FROM nodestro_documents d JOIN nodestro_tenants t ON t.id = d.tenant_id LEFT JOIN nodestro_axentra_ids l ON l.kind = 'tenant' AND l.nodestro_id = d.tenant_id WHERE d.collection = 'usage_log' AND coalesce((d.data -> 'usage' ->> 'requests')::bigint, 0) > 0 AND (d.data ->> 'at')::timestamptz >= now() - make_interval(days => %(days)s) AND NOT EXISTS (SELECT 1 FROM axentra_usage_events e WHERE e.product_id = %(product)s AND e.tenant_id = l.axentra_id AND e.idempotency_key = 'nodestro:usage:' || d.id || ':nodestro.ai.requests')",
    ): ("operator usage reconcile: counts usage-log entries of every tenant missing in Axentra; counts only"),
    (
        "nodestro_runtime/axentra_platform.py",
        "SELECT count(*) FROM nodestro_documents d JOIN nodestro_tenants t ON t.id = d.tenant_id LEFT JOIN nodestro_axentra_ids l ON l.kind = 'tenant' AND l.nodestro_id = d.tenant_id WHERE d.collection = 'runs' AND d.data ->> 'status' IN ('succeeded', 'failed', 'cancelled') AND d.data ->> 'parent_run_id' IS NULL AND d.data ->> 'imported_at' IS NULL AND coalesce((d.data -> 'usage' ->> 'requests')::bigint, 0) > 0 AND d.updated_at >= now() - make_interval(days => %(days)s) AND NOT EXISTS (SELECT 1 FROM axentra_usage_events e WHERE e.product_id = %(product)s AND e.tenant_id = l.axentra_id AND e.idempotency_key = CASE WHEN ('nodestro:run:' || d.id || ':nodestro.ai.requests') ~ '^[!-~]{1,200}$' THEN 'nodestro:run:' || d.id || ':nodestro.ai.requests' ELSE 'nodestro:run:h.' || encode(sha256(convert_to(d.id, 'UTF8')), 'hex') || ':nodestro.ai.requests' END)",
    ): ("operator usage reconcile: counts settled runs of every tenant missing in Axentra; counts only"),
    (
        "nodestro_runtime/axentra_platform.py",
        "SELECT count(*) FROM nodestro_documents d JOIN nodestro_tenants t ON t.id = d.tenant_id LEFT JOIN nodestro_axentra_ids l ON l.kind = 'tenant' AND l.nodestro_id = d.tenant_id WHERE d.collection IN ('tool_approval_audit', 'mcp_events') AND NOT EXISTS (SELECT 1 FROM axentra_audit_events a WHERE a.product_id = %(product)s AND a.tenant_id = l.axentra_id AND a.details ->> 'nodestro_event' = d.data ->> 'id')",
    ): ("operator audit reconcile: counts audit events of every tenant missing in Axentra; counts only"),
    (
        "nodestro_runtime/identity_store.py",
        "SELECT m.tenant_id, m.role, t.status FROM nodestro_memberships m JOIN nodestro_tenants t ON t.id = m.tenant_id WHERE m.user_id = %s ORDER BY m.tenant_id",
    ): ("the tenants of the signed-in user (tenant switch, login): keyed by the user id from the session"),
    (
        "nodestro_runtime/identity_store.py",
        "SELECT s.user_id, s.tenant_id, m.role, s.expires_at, s.last_seen_at, t.status FROM nodestro_sessions s JOIN nodestro_memberships m ON m.tenant_id = s.tenant_id AND m.user_id = s.user_id JOIN nodestro_tenants t ON t.id = s.tenant_id WHERE s.token_hash = %s AND s.expires_at > clock_timestamp() AND s.last_seen_at > clock_timestamp() - %s",
    ): (
        "session lookup by the SHA-256 of the session token, before any tenant is known: the token picks the "
        "one session and its tenant, and the membership join is bound to that tenant (m.tenant_id = "
        "s.tenant_id)"
    ),
    (
        "nodestro_runtime/identity_store.py",
        "UPDATE nodestro_sessions SET last_seen_at = clock_timestamp() WHERE token_hash = %s",
    ): ("touch of the one session whose token hash the caller presented (server-generated secret, unique key)"),
    ("nodestro_runtime/identity_store.py", "DELETE FROM nodestro_sessions WHERE token_hash = %s"): (
        "logout: deletes the one session whose token hash the caller presented"
    ),
    ("nodestro_runtime/identity_store.py", "DELETE FROM nodestro_sessions WHERE user_id = %s"): (
        "revoke-sessions of one user (operator CLI, user removal): every session of that user in every "
        "tenant, by design"
    ),
    (
        "nodestro_runtime/identity_store.py",
        "DELETE FROM nodestro_login_states WHERE state_hash = %s RETURNING nonce, code_verifier, return_to, tenant_id, bootstrap_hash, expires_at > clock_timestamp()",
    ): (
        "OIDC callback: consumes the one login state whose hash the browser presented (single use, before a "
        "tenant is known; the state carries the tenant)"
    ),
    (
        "nodestro_runtime/identity_store.py",
        "UPDATE nodestro_bootstrap_tokens SET used_at = clock_timestamp(), used_by = %s WHERE token_hash = %s AND used_at IS NULL AND expires_at > clock_timestamp() RETURNING tenant_id",
    ): ("setup link: consumes the one bootstrap token whose hash was presented (single use); returns its tenant"),
    (
        "nodestro_runtime/identity_store.py",
        "DELETE FROM nodestro_sessions WHERE expires_at <= clock_timestamp() OR last_seen_at <= clock_timestamp() - %s",
    ): ("housekeeping of the leader: expired and idle sessions of every tenant"),
    ("nodestro_runtime/identity_store.py", "DELETE FROM nodestro_login_states WHERE expires_at <= clock_timestamp()"): (
        "housekeeping of the leader: expired login states"
    ),
    (
        "nodestro_runtime/jobs.py",
        "UPDATE nodestro_jobs SET status = 'running', worker = %s, attempts = attempts + 1, started_at = clock_timestamp(), lease_until = clock_timestamp() + %s WHERE id = (SELECT id FROM nodestro_jobs WHERE status = 'queued' AND kind = ANY(%s) ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING id, tenant_id, kind, ref, status, payload, attempts, worker, lease_until, cancel_requested, error, created_at, started_at, finished_at",
    ): (
        "worker claim: takes the oldest queued job of any tenant (workers serve every tenant); the job row it "
        "returns carries its tenant_id, which binds everything the worker does next"
    ),
    (
        "nodestro_runtime/jobs.py",
        "UPDATE nodestro_jobs SET lease_until = clock_timestamp() + %s WHERE id = %s AND worker = %s AND status = 'running' RETURNING id, tenant_id, kind, ref, status, payload, attempts, worker, lease_until, cancel_requested, error, created_at, started_at, finished_at",
    ): (
        "worker heartbeat: keyed by the server-generated job id (bigserial) plus the claiming worker's name; "
        "touches only the lease of a job this worker claimed"
    ),
    (
        "nodestro_runtime/jobs.py",
        "UPDATE nodestro_jobs SET status = %s, error = %s, finished_at = clock_timestamp(), lease_until = NULL WHERE id = %s AND worker = %s AND status = 'running'",
    ): (
        "worker finish: keyed by the server-generated job id plus the claiming worker's name; only a job this "
        "worker claimed and still runs"
    ),
    (
        "nodestro_runtime/jobs.py",
        "UPDATE nodestro_jobs SET status = 'interrupted', finished_at = clock_timestamp(), error = 'The worker stopped renewing its lease' WHERE id IN (SELECT id FROM nodestro_jobs WHERE status = 'running' AND lease_until < clock_timestamp() FOR UPDATE SKIP LOCKED) RETURNING id, tenant_id, kind, ref, status, payload, attempts, worker, lease_until, cancel_requested, error, created_at, started_at, finished_at",
    ): ("reaper of the leader: marks running jobs of every tenant whose lease expired as interrupted"),
    (
        "nodestro_runtime/jobs.py",
        "SELECT id, tenant_id, kind, ref, status, payload, attempts, worker, lease_until, cancel_requested, error, created_at, started_at, finished_at FROM nodestro_jobs WHERE id = %s",
    ): (
        "JobQueue.get: keyed by the server-generated job id (bigserial); job ids are never taken from a "
        "request (no request handler calls it; the worker uses the id of the job it claimed)"
    ),
    (
        "nodestro_runtime/jobs.py",
        "DELETE FROM nodestro_jobs WHERE status NOT IN ('queued', 'running') AND finished_at < clock_timestamp() - %s",
    ): ("housekeeping of the leader: deletes finished jobs of every tenant older than the retention"),
    ("nodestro_runtime/jobs.py", "DELETE FROM nodestro_run_events WHERE created_at < clock_timestamp() - %s"): (
        "housekeeping of the leader: run events older than the retention, of every tenant"
    ),
    (
        "nodestro_runtime/jobs.py",
        "SELECT d.tenant_id, d.id FROM nodestro_documents d WHERE d.collection = 'runs' AND d.data ->> 'status' IN ('queued', 'running') AND d.data ->> 'parent_run_id' IS NULL AND d.data ->> 'imported_at' IS NULL AND d.updated_at < clock_timestamp() - %s AND NOT EXISTS (SELECT 1 FROM nodestro_jobs j WHERE j.tenant_id = d.tenant_id AND j.kind = %s AND j.ref = d.id AND j.status IN ('queued', 'running')) ORDER BY d.tenant_id, d.id",
    ): (
        "reaper of the leader: finds stored runs of every tenant that no job executes (returns (tenant, run) "
        "pairs that are then handled per tenant); the jobs subquery is bound to each run's tenant"
    ),
    ("nodestro_runtime/postgres_import.py", "nodestro_documents"): (
        "not SQL: the table name passed to _has_table() on the SQLite file being imported"
    ),
    (
        "nodestro_runtime/postgres_import.py",
        "SELECT collection, id, data, updated_at FROM nodestro_documents ORDER BY collection, id",
    ): (
        "reads the desktop's SQLite file being imported (single tenant, no tenant_id column); the rows are "
        "written with the target tenant_id"
    ),
    (
        "nodestro_runtime/storage_postgres.py",
        "SELECT id, coalesce(raw, data::text) FROM nodestro_documents WHERE tenant_id = %s AND collection = %s AND jsonb_typeof(data -> '{field}') = 'string' AND data ->> '{field}' = ANY(%s) ORDER BY updated_at, id",
    ): (
        "find(): {field} is checked against _FIELD ([A-Za-z_][A-Za-z0-9_]*) before it is used, so it is a "
        "JSON key literal and cannot change the statement; the tenant is bound by tenant_id = %s (kept as a "
        "literal so the expression indexes of migrations 0001/0006 apply)"
    ),
    (
        "nodestro_runtime/axentra_platform.py",
        "SELECT {[sql.Identifier(name) for name in names]} FROM (SELECT {[sql.Identifier(name) for name in names]}, row_number() OVER (PARTITION BY split_part({column.column}, '.', 2) ORDER BY {column.key_column}::text) AS sample FROM {column.table} WHERE {column.column} LIKE %s) AS sampled WHERE sample <= %s",
    ): (
        "start check (AxentraServices.key_samples, SR-04a): reads up to five values per key id of each of "
        "Axentra's own keyring columns (platform.encrypted_columns: login states, webhook secrets, notification "
        "messages and mailbox) to prove the key bytes; the identifiers come from that list, names no Nodestro "
        "tenant table, returns nothing of a value"
    ),
    (
        "nodestro_runtime/tenant_sql_check.py",
        "{ref.table} (as {ref.name}) is not bound to one tenant by a top-level `tenant_id = <param>` conjunct",
    ): ("not SQL: a problem message of the check itself (an unknown part followed by SQL words)"),
    (
        "nodestro_runtime/tenant_sql_check.py",
        "{ref.table} is not bound to one tenant by a top-level `tenant_id = <param>` conjunct",
    ): ("not SQL: a problem message of the check itself (an unknown part followed by SQL words)"),
}
