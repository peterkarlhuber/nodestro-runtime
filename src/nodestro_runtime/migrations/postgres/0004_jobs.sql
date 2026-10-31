-- Jobs, run events and schedule firings of the server profile (ADR-0004). API processes enqueue work; worker
-- processes claim it with a lease they keep renewing. A job whose lease expires is never executed again: its run is
-- marked interrupted and can be continued by a person. Run events are kept here so that every API process can stream
-- the events of a run that executes in a worker.

CREATE TABLE nodestro_jobs (
    id bigserial PRIMARY KEY,
    tenant_id text COLLATE "C" NOT NULL,
    kind text COLLATE "C" NOT NULL,
    ref text COLLATE "C" NOT NULL,
    status text NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled', 'interrupted')),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    attempts integer NOT NULL DEFAULT 0,
    worker text,
    lease_until timestamptz,
    cancel_requested boolean NOT NULL DEFAULT false,
    error text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    started_at timestamptz,
    finished_at timestamptz,
    CHECK (tenant_id <> '' AND kind <> '' AND ref <> '')
);
-- At most one active job per piece of work: a run is never queued or executed twice at the same time.
CREATE UNIQUE INDEX nodestro_jobs_active ON nodestro_jobs (tenant_id, kind, ref) WHERE status IN ('queued', 'running');
CREATE INDEX nodestro_jobs_queued ON nodestro_jobs (kind, id) WHERE status = 'queued';
CREATE INDEX nodestro_jobs_leases ON nodestro_jobs (lease_until) WHERE status = 'running';

CREATE TABLE nodestro_run_events (
    tenant_id text COLLATE "C" NOT NULL,
    run_id text COLLATE "C" NOT NULL,
    seq bigint NOT NULL,
    -- The event as JSON text (not jsonb: an event may carry U+0000 in model output).
    event text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, run_id, seq)
);
CREATE INDEX nodestro_run_events_age ON nodestro_run_events (created_at);

CREATE TABLE nodestro_schedule_firings (
    tenant_id text COLLATE "C" NOT NULL,
    schedule_id text COLLATE "C" NOT NULL,
    slot timestamptz NOT NULL,
    run_id text COLLATE "C",
    fired_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, schedule_id, slot)
);
