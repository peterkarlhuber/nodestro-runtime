# JQ-2: writes repeated by `PostgresDatabase._query` after a lost reply

Impact analysis for work packages JQ-1 to JQ-3 (planning document 06, section "Job queue review", questions of the
JQ-2 table). Each answer cites the test that shows it. Line numbers refer to master at 816b3bc, before the fix.

## Status

- The mechanism was **reproduced under fault injection** (PostgreSQL 16 locally, with the tests below). It was
  **not observed in operation**, so this is not a confirmed production defect.
- It contradicts RT2-03 (`storage_postgres.py`, `PostgresDatabase` and `_connection`: "a failed write is not
  repeated").
- Fixed by JQ-3 (option A, no schema change, no public signature change); see "The fix" below.

## Mechanism

`PostgresDatabase._query` (`storage_postgres.py:385-403`) runs a statement outside a transaction in autocommit. When
it meets `psycopg.OperationalError` and the connection is `broken`, it runs the same statement again on another
pooled connection, up to `pool_size + 1` times. For a read that is correct. A writing statement may already have been
executed and committed by the server when its reply is lost: the repeat then runs a second time against the changed
data, and its answer is returned in place of the first one. `_write` (`:405-416`) never repeats.

An AST scan of every `._query(` call found 71 calls (68 in the runtime at 816b3bc, 3 in nodestro-api at 8fbf220;
planning document 06 counted 74 at its own state); 9 of them write, all in the runtime (nodestro-api's three calls in
`server.py` are reads):

| # | Site (816b3bc) | Statement | Effect of the repeat after the commit | Class |
|---|---|---|---|---|
| 1 | `jobs.py:135` `JobQueue.enqueue` | `INSERT … ON CONFLICT DO NOTHING RETURNING` | `None`: "already queued" | wrong answer |
| 2 | `jobs.py:145` `JobQueue.claim` | `UPDATE … FOR UPDATE SKIP LOCKED RETURNING` | a second job is claimed; the first runs with nobody executing it | **lost job** |
| 3 | `jobs.py:157` `JobQueue.heartbeat` | `UPDATE lease RETURNING` | the lease is renewed again | idempotent |
| 4 | `jobs.py:198` `JobQueue.reap_expired` | `UPDATE → interrupted RETURNING` | `[]`: the reaped jobs are not reported | delayed healing |
| 5 | `jobs.py:210` `JobQueue.report` | `UPDATE payload \|\| jsonb RETURNING` | the same keys are merged again | idempotent |
| 6 | `identity_store.py:130` `IdentityStore.create_tenant` | `INSERT … ON CONFLICT DO NOTHING RETURNING` | `identity.tenant_exists` for the new tenant | wrong answer |
| 7 | `identity_store.py:161` `IdentityStore.set_tenant_status` | `UPDATE RETURNING` | the same status again | idempotent |
| 8 | `identity_store.py:175` `IdentityStore.login` | `INSERT … ON CONFLICT DO UPDATE RETURNING id` | the same user id | idempotent |
| 9 | `identity_store.py:328` `IdentityStore.take_login_state` | `DELETE … RETURNING` | `None`: the login fails as expired | fail-safe, wrong message |

Not affected: `JobQueue.request_cancel` (`jobs.py:176-194`, in `_transaction`), `JobQueue.finish` and
`JobQueue.cleanup` (`_write`), and `DatabaseSource` in `read` mode (`storage_postgres.py:742-751`; Orkexa's backends
run only reads in it).

## How it was tested

`tests/postgres_support.py` has `FaultProxy` and the `fault_proxy` fixture: an in-process TCP proxy in front of the
test server that follows the protocol's message framing. Armed with a part of a statement's SQL, it forwards the
statement, drops every server message up to and including the `ReadyForQuery` that ends it, and then closes both
sides (`lose="reply"`: the statement was applied and committed, the client sees a lost connection), or it drops the
request itself (`lose="request"`). The cut is placed by the server's own end-of-statement message, never by a timer;
no test sleeps (leases are expired by moving `lease_until` into the past). Every test asserts that the fault fired.

`tests/test_write_retry.py` (25 tests) covers all nine sites, the counter-probes and the reads. Before the fix,
commit f927e1a ran the ten cases whose outcome the repeat changes as `xfail(strict=True)`; with `--runxfail` they
failed as listed in "Test results". The suite ran 20 times in a row without a flake before and after the fix, as the
superuser and as the restricted runtime role (`NODESTRO_TEST_RUNTIME_ROLE=1`). CI runs it on PostgreSQL 17.

## Answers to the JQ-2 questions

### Double processing?

**No.** A repeated claim takes a *different* job (`FOR UPDATE SKIP LOCKED` on queued rows only, and the partial
unique index allows one active job per piece of work), so no job is executed by two workers.
Evidence: `test_a_lost_claim_never_gives_one_job_to_two_workers` (a worker's claim loses its reply, a second worker
claims next; the two jobs differ and no job has more than one attempt). Passes before and after the fix.

### Lost job?

**Yes, before the fix (site 2).** The repeat claims the next queued job (or none) and returns it; the first job stays
`running` for a worker that does not know it has it. Nobody executes it; after its lease expires, the reaper marks it
and its run `interrupted`, and the run waits for a person to continue it.
Evidence: `test_a_claim_whose_reply_is_lost_returns_the_claimed_job` (before the fix: both jobs `running` for `w1`,
the second one returned) and `test_a_worker_whose_claim_reply_is_lost_executes_the_run` (end to end with
`RunWorker`: before the fix the run stayed `queued`, its job `running` without an executor, and `run_once` returned
`None`; after the fix the run succeeds and its job is `done`).

### `run.already_queued`

**Yes, before the fix (site 1).** `QueuedRuns.dispatch` (`jobs.py:341-343`) raises `run.already_queued` when
`enqueue` returns `None`, although `RunManager._launch` (`engine.py:1187-1204`) has stored the run as `queued` and the
job exists, so a worker executes the run anyway. The API answers a `NodestroError` without its own status with
HTTP 400 and the code (`nodestro-api` `api.py:155-161`, code reading): the caller is told the start failed while the
run goes on. How the studio shows it was not checked in this work package.
Evidence: `test_a_dispatch_whose_reply_is_lost_does_not_report_run_already_queued` and
`test_an_api_start_whose_enqueue_reply_is_lost_succeeds` (a `Workspace.run` through `QueuedRuns`; before the fix it
raised `run.already_queued` with the job queued).

### Export (nodestro-api)

**Yes, before the fix (site 1, export path).** `QueuedExports.enqueue` (`export_jobs.py:91-94`) raises `export.busy`
when `enqueue` returns `None`; `JobRunner._enqueue` (`portable_build.py:401-406`) then **deletes the export job
document** although the queue row exists. The export worker claims that row, finds no document and ends the queue job
as `failed` with `export.failed: the export job is gone` (`export_jobs.py:210-212`). The user sees `export.busy` for
an export that was never built; the next export can be started (the queue row has ended).
Evidence: the runtime side is `test_an_enqueue_whose_reply_is_lost_returns_the_queued_job` (before the fix
`enqueue` returned `None` with the job queued). The API side was checked with a scratch probe (not committed; it runs
nodestro-api 8fbf220's `QueuedExports` and `ExportWorker` against the fault proxy, with `JobRunner._enqueue`'s
store-then-delete steps):

```text
before the fix: fired 1 | raised export.busy; document deleted | documents [] | queue [('exp_1', 'queued', None)]
                worker claimed exp_1 | queue [('exp_1', 'failed', 'export.failed: the export job is gone')]
after the fix:  fired 1 | enqueue returned | documents ['exp_1'] | queue [('exp_1', 'queued', None)]
                worker claimed exp_1 and found the document (it refused it with export.server_unsupported,
                because the probe does not enable server exports)
```

After the fix `enqueue` returns the queued job, so no `export.busy` is raised and nothing is deleted. A regression
test on the API side belongs to the export part of JQ-4 in nodestro-api. `portable_build.py:401-406` itself
needs no change for this case. It still deletes the document when `enqueue` *raises* (for example the
`OperationalError` of a lost connection that the reconciliation cannot resolve); then no queue row exists, which is
consistent.

### Reindex

**Yes, before the fix (site 1).** `QueuedReindex.start` (`jobs.py:398-400`) raises `knowledge.reindex_running`
although the reindex job it queued is running. The UI is told a reindex is already running, which is true, but it was
started by this very request; the knowledge base's state comes from the queue (`QueuedReindex.current`), so it shows
the job.
Evidence: `test_a_reindex_whose_reply_is_lost_does_not_report_reindex_running`.

### Healing

- Lost claim (before the fix): the orphaned job's lease expires (worker default 60 s) and the reaper (every 15 s,
  `nodestro-api` `worker.py:103`) marks the job and the run `interrupted`; the run is not executed again
  automatically (by design). Evidence: `test_a_claim_whose_reply_is_lost_returns_the_claimed_job` (the state) and
  `test_the_run_of_a_lost_reap_is_healed_by_the_orphan_check` (the reaper path).
- Lost reap: the jobs it marked are not reported, so `reap` does not interrupt their runs. The next reaper finds those
  runs stored as `queued`/`running` without an active job (C-11, `jobs.py:778-791`) and interrupts them once they have
  not changed for twice the lease (default 120 s, plus up to one reap interval). Evidence:
  `test_the_run_of_a_lost_reap_is_healed_by_the_orphan_check` (with `orphans_after=0`). This healing is the same
  before and after the fix; the fix makes the lost reap raise instead of returning `[]`, so it is logged
  (`worker.py:479-486` logs a failed periodic duty and runs it again). Export builds of such jobs are settled by
  `QueuedExports.settle` when the export is next read; their work folder is not removed by `interrupt_reaped`
  (unchanged, a possible follow-up in nodestro-api).

### Frequency

Only when a connection is lost after the server executed a write and before its reply reached the client (a network
cut, a proxy or load balancer closing an active connection, a failover at that moment). There are no operational
data; the assumption is that it is rare. The checkout check of RT2-03 discards connections that the server closed
while they sat idle, so it removes the common cause of a lost *request* after a restart; it cannot help with a reply
lost in flight.

## The fix (JQ-3)

- `PostgresDatabase._write_returning(query, params, *, if_lost=None)` runs one writing statement with `RETURNING` as
  `_write` runs a write: inside the calling thread's transaction, or in autocommit under the process lock, and never
  repeated. `if_lost` is a read that finds what the write did when the connection was lost outside a transaction;
  its rows stand for the lost reply, no rows mean the error is raised. A statement timeout (connection still usable,
  statement rolled back) is raised as it is.
- Site 1 `enqueue`: after a lost connection, the work's active job. If the work was already queued before, that job is
  returned instead of `None`; either way the work is queued, which is what every caller needs. With no active job the
  error is raised.
- Site 2 `claim`: the newest running job of this worker name and these kinds that this `JobQueue` has not handed out
  yet. The queue remembers the jobs it handed out until `finish` or a lost lease (`heartbeat` returning `None`), and
  runs its claims one at a time, so a held job is never handed out twice
  (`test_a_lost_claim_never_returns_a_job_the_worker_already_holds`). This relies on worker names being unique per
  process (`worker_name()`), as `heartbeat` and `finish` already do. With no such job the claim was not applied and
  the error is raised; the job stays queued (`test_a_claim_whose_request_is_lost_fails_and_leaves_the_job_queued`).
  The reconciliation is unambiguous under that rule, so option B (an idempotency token, a schema change) is not
  needed.
- Site 4 `reap_expired` and site 9 `take_login_state`: the error is raised (the next reaper and C-11 heal; a login
  state is used up either way, so the user starts the login again).
- Site 6 `create_tenant`: after a lost connection, the stored tenant (created now, or existing before with the same
  id; the latter is then returned instead of `identity.tenant_exists`).
- Sites 3, 5, 7, 8 stay on `_query`, with a comment at each call: a repeat has the same effect and the same answer.
- Guard: `tests/test_query_writes.py` resolves the SQL text of every `._query(...)` call in the runtime (literals,
  f-strings, `+`, names) and fails for a writing statement or an unresolved text that is not in its allowlist (the
  four idempotent sites, `PostgresStore.lock` inside a transaction, `DatabaseSource`'s read mode and the
  reconciliation read of `_write_returning`, each with its reason). The `if_lost` reads must be reads. Against the
  code before the fix it reports exactly sites 1, 2, 4, 6 and 9.

Behavior that changes with the fix: a write of sites 1, 2, 4, 6 and 9 whose connection is lost **before** it reached
the server (and that the reconciliation cannot show to be applied) now fails with `psycopg.OperationalError` instead
of being repeated, as RT2-03 says for every write (`test_an_enqueue_whose_request_is_lost_fails_and_queues_nothing`).
These writes also run under the process lock now, like every other write of a process.

## Test results

Before the fix (f927e1a, with `--runxfail`), after the fix (658a200):

| Test | Site | Before | After |
|---|---|---|---|
| `test_an_enqueue_whose_reply_is_lost_returns_the_queued_job` | 1 | fail (`None`) | pass |
| `test_a_dispatch_whose_reply_is_lost_does_not_report_run_already_queued` | 1 | fail (`run.already_queued`) | pass |
| `test_a_reindex_whose_reply_is_lost_does_not_report_reindex_running` | 1 | fail (`knowledge.reindex_running`) | pass |
| `test_an_api_start_whose_enqueue_reply_is_lost_succeeds` | 1 | fail (`run.already_queued`) | pass |
| `test_a_claim_whose_reply_is_lost_returns_the_claimed_job` | 2 | fail (two jobs running for `w1`) | pass |
| `test_a_worker_whose_claim_reply_is_lost_executes_the_run` | 2 | fail (run stays `queued`) | pass |
| `test_a_lost_claim_never_gives_one_job_to_two_workers` | 2 | pass | pass |
| `test_a_heartbeat_whose_reply_is_lost_renews_the_lease` | 3 | pass | pass |
| `test_a_reap_whose_reply_is_lost_fails_instead_of_reporting_nothing` | 4 | fail (returned `[]`) | pass |
| `test_the_run_of_a_lost_reap_is_healed_by_the_orphan_check` | 4 | fail (returned `[]`) | pass |
| `test_a_report_whose_reply_is_lost_merges_the_progress_once` | 5 | pass | pass |
| `test_a_tenant_creation_whose_reply_is_lost_returns_the_tenant` | 6 | fail (`identity.tenant_exists`) | pass |
| `test_a_tenant_status_change_whose_reply_is_lost_returns_the_new_status` | 7 | pass | pass |
| `test_a_first_login_whose_reply_is_lost_returns_the_one_stored_user` | 8 | pass | pass |
| `test_a_login_state_whose_reply_is_lost_fails_instead_of_reporting_it_expired` | 9 | fail (returned `None`) | pass |
| `test_a_finish_whose_reply_is_lost_fails_and_is_not_repeated` | counter-probe | pass | pass |
| `test_a_cleanup_whose_reply_is_lost_fails_and_is_not_repeated` | counter-probe | pass | pass |
| `test_a_cancel_whose_reply_is_lost_fails_and_changes_nothing` | counter-probe | pass | pass |
| `test_a_read_whose_reply_is_lost_is_repeated_on_another_connection` | read retry | pass | pass |
| `test_a_read_whose_request_is_lost_is_repeated_on_another_connection` | read retry | pass | pass |
| `test_an_enqueue_whose_request_is_lost_fails_and_queues_nothing` | 1 (JQ-3) | – | pass |
| `test_a_claim_whose_request_is_lost_fails_and_leaves_the_job_queued` | 2 (JQ-3) | – | pass |
| `test_a_lost_claim_never_returns_a_job_the_worker_already_holds` | 2 (JQ-3) | – | pass |
| `test_a_tenant_creation_whose_request_is_lost_fails_and_creates_nothing` | 6 (JQ-3) | – | pass |
| `test_a_write_inside_a_transaction_joins_it_and_is_not_reconciled` | JQ-3 | – | pass |
| `tests/test_query_writes.py` (22 tests, the guard) | all | fails on sites 1, 2, 4, 6, 9 | pass |
