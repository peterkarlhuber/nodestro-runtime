# Axentra in the server profile

Axentra is the generic SaaS backend library of the platform (identity, tenants, permissions, audit, usage, keyring,
storage, jobs, ...). Nodestro uses it as a **Python library inside the server profile only**. The desktop app and
Nodestro Local Docker never import it and never need the package.

Dependency direction: Nodestro → Axentra and Nodestro → Orkexa. Axentra knows nothing about Nodestro or Orkexa; the
Nodestro-specific parts (the product slug, the meters, the action names, the link table) live here.

Everything is **off by default**. Switching it on changes no external contract: login routes, session cookies,
API keys, tenant ids, run and workflow ids, the REST API and the SDK stay as they are.

## Installation and Python versions

`nodestro-runtime[postgres]` (and `[dev]`) installs `axentra[oidc]` from a pinned commit, with the marker
`python_version >= '3.11'`. Axentra needs Python 3.11; Nodestro still supports 3.10 for the desktop, Local Docker
and library use. On 3.10 the server starts without Axentra and refuses `NODESTRO_AXENTRA=on` with
`profile.invalid`. The server image uses Python 3.13 and so always has the package. Raising Nodestro's own minimum
to 3.11 is tracked as RT-022.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `NODESTRO_AXENTRA` | `off` | `on`: open Axentra in the same PostgreSQL database (tables `axentra_*` next to `nodestro_*`), one product `nodestro`, single-product mode |
| `NODESTRO_AXENTRA_KEYRING` / `_FILE` | – | Axentra keyring `kid:base64key[,kid2:...]`; the first key encrypts. Required with `on` |
| `NODESTRO_AXENTRA_CONFIG` | – | Optional Axentra TOML file for Axentra's own knobs; Nodestro's values (database, product, mode, keyring) always win. `AXENTRA__*` variables are ignored |
| `NODESTRO_AXENTRA_ALLOW_SUPERUSER` | `0` | `1` lets Axentra run on a superuser or `BYPASSRLS` role. Its row-level security is then no defense in depth; production uses a dedicated role |
| `NODESTRO_SECRETS_FORMAT` | `v1` | `axs1`: new tenant secrets are written with the Axentra keyring (rotation). Needs `on` |
| `NODESTRO_AXENTRA_AUDIT` | `0` | `1`: mirror the approval audit trail and MCP events into Axentra's audit log. Needs `on` |
| `NODESTRO_AXENTRA_USAGE` | `0` | `1`: mirror the AI usage ledger into Axentra meters `nodestro.ai.*`. Needs `on` |
| `NODESTRO_AXENTRA_RUN_USAGE` | `0` | `1`: also settle the usage of finished workflow runs into the same meters, once per run; see [Usage](#usage-end-state-and-run-usage). Needs `NODESTRO_AXENTRA_USAGE=1` |
| `NODESTRO_AXENTRA_NOTIFY` | `0` | `1`: in-app inbox notifications (failed run, pending tool approval, budget decision) through Axentra's notify module; never e-mail. Needs `on`; see [Notifications](#notifications) |
| `NODESTRO_METRICS_TOKEN` / `_FILE` | – | Bearer token of `GET /api/metrics` (at least 32 characters); unset: no endpoint. Needs the package, not `on`; see [Observability](#observability-m7) |
| `NODESTRO_IDENTITY_BACKEND` | `nodestro` | `mirror`: Nodestro answers, every identity write also goes to Axentra in the same transaction; `axentra`: Axentra answers, both are written. Needs `on`; see [Identity](#identity-and-tenancy-m5) |

Every invalid combination stops the server with `profile.invalid` and names every problem.

## Schema and migrations

`nodestro-server migrate` applies Nodestro's migrations and then Axentra's (namespaced, checksummed, under Axentra's
advisory lock). One backup (`--backup-dir` runs `pg_dump`, `--backup-taken` states one was made) covers both runners;
a new, empty database needs none. `check`, `api` and `worker` refuse to start while either schema is behind
(`storage.schema_mismatch`). Switching Axentra off again needs nothing: its tables stay and are not read.

Nodestro migration `0007_axentra_links` adds `nodestro_axentra_ids(kind, nodestro_id, axentra_id)`: the only
mapping between Nodestro ids and Axentra ids (`tenant`, `user`, `session`, `api_key`), unique in both directions.
Nodestro ids never change.

## Tenant secrets and key rotation

`KeyringSecretBox` reads both formats (`nodestro:enc:v1:...` and `axs1.<kid>....`) and writes the configured one, so
changing the format never makes a stored value unreadable. Each `axs1` value is bound to its tenant and field
(`nodestro|tenant:<id>|<field>` as AES-GCM associated data): a value copied to another tenant or field does not
decrypt.

Rotation:

1. Add the new key in front of the keyring (`k2:...,k1:...`) and restart: new values use `k2`, old ones stay readable.
2. `nodestro-server secrets reencrypt --dry-run` counts the values per format; `secrets reencrypt` moves every
   provider key, MCP token, pending OAuth state and billing admin key to the active key. One transaction per
   document with a row lock; resumable and idempotent; `--tenant` limits it.
3. A value that cannot be read is **never overwritten or dropped**: it is reported (`tenant/collection/key:field`, no
   values) and the command exits with code 2.
4. Remove the old key only after a run reports `pending: 0` and `failed: []`.

The same procedure moves v1 values to the keyring (`NODESTRO_SECRETS_FORMAT=axs1`). Until every v1 value is
re-encrypted, `NODESTRO_SECRET_KEY` must stay set.

## Tenants

`AxentraLink.ensure_tenant` creates the Axentra tenant of a Nodestro tenant (slug derived from the id, `-2`, `-3` on a
collision, `settings.nodestro_tenant` = the Nodestro id) in the caller's Nodestro transaction, serialized by an
advisory lock; `backfill_tenants` links every existing tenant (idempotent, resumable). Pseudo tenants without a
`nodestro_tenants` row (the price reference) are never linked.

## Audit and usage mirrors (M3)

Nodestro's own records stay the source of truth: the approval audit trail, the MCP event log and the usage ledger
are written exactly as before, and the usage dashboard reads them. The mirrors add a copy in Axentra:

- **One transaction, one connection.** The record and its mirror are written on Nodestro's connection with Axentra's
  services joined to it (`Database.joined`, `join`): a rolled-back Nodestro transaction leaves no Axentra rows, and
  nothing is ever dual-written across two connections.
- **Failure isolation.** The mirror runs in a savepoint. When it fails (Axentra unavailable, a missing meter), only
  the savepoint is rolled back and the failure is logged; the Nodestro record is kept.
- **Reconciliation.** `nodestro-server axentra reconcile [--dry-run] [--tenant ID]` writes what is missing: a failed
  mirror, or records from before the switch. Usage uses idempotency keys `nodestro:usage:<entry>:<meter>`, so
  nothing is ever counted twice; audit events are matched by `details.nodestro_event`. Usage older than 395 days is
  reported (`usage_too_old`), not written, because Axentra refuses it. Exit code 2 when a write failed.
- **What is mirrored.** Audit: every tool approval event (`nodestro.tool_approval.<kind>`; `decision_refused` has the
  outcome `denied`) and every MCP event (`nodestro.mcp.<kind>`), with ids, tool, result and the arguments' hash.
  Never the call arguments, never secrets; MCP addresses are redacted. The actor is the linked Axentra user, or the
  system actor with the Nodestro actor id in `actor_display`. Usage: the meters `nodestro.ai.requests`,
  `nodestro.ai.input_tokens`, `nodestro.ai.output_tokens` and `nodestro.ai.cost_usd` (monthly, sum). The cost is
  mirrored only when it is complete; an unknown or partial cost is never billed as the whole cost.
- **Workflow runs** are mirrored only with `NODESTRO_AXENTRA_RUN_USAGE=1` (see
  [Usage](#usage-end-state-and-run-usage)); otherwise their usage stays in the stored runs only. No limit is
  enforced from Axentra (`enforce=False`).

## Identity and tenancy (M5)

`NODESTRO_IDENTITY_BACKEND` chooses where users, tenants, memberships and sessions live
(`nodestro_runtime.axentra_identity`). A change is a restart of `api` and `worker`.

| Value | Reads from | Writes to |
|---|---|---|
| `nodestro` (default) | Nodestro's tables (`IdentityStore`, unchanged) | Nodestro only |
| `mirror` | Nodestro's tables | both, **one PostgreSQL transaction** (Nodestro's connection, Axentra joined to it) |
| `axentra` | Axentra: session validity (absolute/idle expiry, revocation, disabled user; database clock), roles, memberships, member lists, tenant list | both, one transaction (Nodestro's tables stay current for the rollback) |

`AxentraIdentityStore` has `IdentityStore`'s interface; `nodestro-server` builds the store for `api`, `worker` and the
member commands in one place. A failure on either side rolls back both; there is no dual write across connections.

**Mapping.**

- Tenant: Axentra tenant of product `nodestro`, linked in `nodestro_axentra_ids` (kind `tenant`); the slug is only a
  label (`acme_eu` -> `acme-eu`, `a` -> `a-0`, collisions `-2`, ...). Names longer than 200 characters are cut.
- User: Axentra user created with `create_external_user`, identity `(provider = OIDC issuer, subject)` exactly as
  Nodestro keys users; linked as kind `user`. **Never merged by e-mail**: Axentra stores the (verified) address only
  when no other Axentra user has it (and Axentra accepts its form); otherwise the user has no address in Axentra and
  the identity row keeps it for reference. Nodestro keeps its own `email` column. The oldest account keeps a shared
  address. No Axentra OIDC provider entry is needed: Axentra does not run the login (Nodestro's OIDC flow and routes
  are unchanged), and `create_external_user` takes the issuer string as the provider.
- Roles: `owner` -> `tenant_owner` (Axentra's last-owner guard applies too), `admin` -> `tenant_admin`, `viewer` ->
  `viewer`, `editor`/`operator` -> custom tenant roles of the same key in product `nodestro` with generic tenant
  permissions only (no `members:manage`, no tenant settings), created idempotently at start. Nodestro's
  `read/edit/run/admin` still come from `ROLE_PERMISSIONS` by Nodestro role name; `/api/auth/me` is unchanged.
- Sessions: the same token. Axentra's `token_hash` is the hex form of Nodestro's `bytea` SHA-256, so the hash joins
  both rows (no mapping row). New sessions are started by Axentra (`start_session` makes the token, Nodestro stores
  its hash); both rows get Nodestro's expiry. The active tenant stays `nodestro_sessions.tenant_id`. `last_seen_at`
  touches, logout, "log out everywhere", removal of a member (ends their sessions in that tenant at once) and
  housekeeping are mirrored. A tenant switch still issues a new session and ends the old one.
- Stays Nodestro-only: login states (10 minutes; a login started during the switch fails once with
  `identity.login_expired`) and bootstrap tokens (setup links; using one writes the owner membership on both sides).

**Procedure (maintenance window).** Stop `api` and `worker`; back up (`migrate --backup-dir`); set `NODESTRO_AXENTRA=on`
and `migrate`; `nodestro-server axentra backfill-identity --dry-run`, then without `--dry-run` (idempotent, resumable:
one transaction per tenant, user, tenant's memberships, batch of sessions; reports created records, slug changes and
users stored without an address); `nodestro-server axentra reconcile-identity` must print `"ok": true` (exit code 1
otherwise; ids and counts only, never tokens or hashes); set `NODESTRO_IDENTITY_BACKEND=mirror`, start. Later
`axentra` (restart). `api`, `worker` and `check` refuse `axentra` with `identity.backfill_required` while a quick count
finds records missing on one side, and refuse an Axentra session idle limit that differs from Nodestro's 2 hours.
Nobody has to log in again: sessions are copied with their hash.

**Rollback.** `axentra` -> `mirror` -> `nodestro` is a restart each; Nodestro's tables are current in both modes.
Writes made in `nodestro` mode after a switch back are not mirrored: run the backfill again before switching forward.

**Limits.** Session resolution in `axentra` mode costs three short reads instead of one; when Axentra notes a use on
its own (`resolve_session`, at most once a minute) Nodestro's `last_seen_at` may lag up to about two minutes (it only
matters for idle expiry after a rollback). In `axentra` mode the tenant list, roles and members come from Axentra,
the user profile (`/api/auth/me` name and address) from Nodestro's row. A suspended Axentra tenant is not yet treated
as closed. Axentra's own HTTP API and admin functions are not exposed through Nodestro.

## Usage: end state and run usage

**Division of labour (end state).** Axentra is the generic SaaS meter: meters, idempotent usage events, period
counters, quotas, reservations (all already in Axentra's `UsageService`). Everything AI-specific stays Nodestro's
and Orkexa's: prices and price snapshots, token and cost calculation, per-run budgets and their admission, provider
cost reconciliation (`reconciliation.py`), price quality. Axentra receives finished quantities only:
`nodestro.ai.requests`, `.input_tokens`, `.output_tokens` and `.cost_usd` (micro-dollars, only when the cost is
complete: an unknown or partial cost is never metered as the whole cost).

**Run usage (`NODESTRO_AXENTRA_RUN_USAGE=1`).** A run's usage is mirrored once, when it is settled: the run is
`succeeded`, `failed` or `cancelled`. Same filter as the usage dashboard: top-level runs only (a sub-workflow's
cost is counted in its parent) and never imported runs. Each meter gets one usage event with the key
`nodestro:run:<run id>:<meter>`, `occurred_at` = the run's `finished_at` and deterministic properties (`source`
`workflow_run`/`api_run`, `status`, `workflow_id`), so the request is identical on every replay.

Why this key never double counts:

- A finished run never changes: continuing a failed run creates a **new** run (`resumed_from`) whose reused steps
  count no cost (`resume.py`), so each run is settled exactly once with its own usage.
- Retries, crashes and parallel workers replay the same key with the same request: Axentra returns the stored event
  and adds nothing (`ON CONFLICT DO NOTHING` + request hash). A crash after the write and before anything was
  noted is harmless for the same reason.
- Should a finished run's stored usage change anyway, the key is already used: the run is reported as `changed`
  (`runs_changed` in `reconcile`, metric `mirror_write{outcome="changed"}`) and never counted again.
- Migration: the key contains only Nodestro's run id, which never changes.

Who writes it: the worker that holds the scheduler lock settles the runs changed since its last pass every 30 s
(the first pass after a start looks 7 days back; the `(tenant, collection, updated_at)` index serves the scan);
`nodestro-server axentra reconcile` settles every finished run Axentra still accepts (395 days; older runs are
reported as `usage_too_old`). Tests: `tests/test_axentra_obs.py` (settle once, crash after write, replay by a fresh
process, resume, changed usage, four concurrent settlers, reconcile twice) and nodestro-api
`tests/test_server_obs.py` (two worker passes).

**Historical data and reports.** The usage dashboard (`GET /api/usage`) and every Nodestro report keep reading
Nodestro's records (runs and the usage ledger): they are the source of truth, carry the AI detail Axentra's meters
do not (models, price quality, stale prices, per-workflow buckets) and cover the time before the switch. Axentra's
meters are a second, generic view for quotas and a later billing integration; history is copied by `reconcile` as
far as Axentra accepts it (395 days), nothing older is backfilled. Moving the dashboard to Axentra is not planned.

**Enforcement.** Not switched on: every write uses `enforce=False` (`record_in`). Axentra's `check_and_record` and
reservations would give hard limits (a row lock on the period counter), but a limit decided after a run finished
cannot stop the run; enforcement belongs in the budget admission before each model call (Nodestro's
`PricedTracker`), with a reservation per run released at settlement. That needs its own design and tests (no
double reservation on resume, release on crash via reservation expiry) and a billing decision; until then quotas
are informational.

## Notifications

`NODESTRO_AXENTRA_NOTIFY=1` switches on Axentra's notify module with **only** its in-app `inbox` channel
(`nodestro_runtime.axentra_notify`). Nodestro forces `notify.provider = "local"` and creates three inbox templates
(`nodestro.run_failed`, `nodestro.approval_pending`, `nodestro.budget_pending`); an inbox message is stored for its
user and never handed to a delivery provider, so nothing leaves the server. If one of these templates is switched
to another channel in Axentra's own admin interface, the worker refuses to start with `axentra.notify_channel`.
E-mail or webhooks would be a separate, explicit switch (not implemented).

- **Events.** A top-level run failed (also `run.interrupted` by the reaper); a tool call waits for an approval; a
  paused run waits for a budget decision (`budget.pending_request_id`; tolerated when absent).
- **Recipients.** The tenant's members with the `run` permission (`owner`, `admin`, `editor`, `operator`) who have a
  linked Axentra user (identity backfill). Viewers get nothing. Content: ids, workflow and tool names and the problem
  code; never call arguments, model output, error texts or secrets.
- **Exactly once per event.** The worker that holds the scheduler lock sweeps what changed since its last pass
  (first pass after a start: one hour back). Each message is keyed `nodestro:notify:<event>:<record>:<user>`, so a
  retried, resumed or crashed worker replays the key and Axentra returns the stored message.
- **Reading.** `GET /api/notifications[?unread_only=true]` and `POST /api/notifications/read` (`{"ids": [...]}` or
  `{}` for all), only with the switch on, `read` permission, the member's own messages in the session's tenant.
  The Studio has no inbox view yet. Contents are erased after Axentra's `notify.content_retention_days` (worker
  cleanup).
- **What stays as it is.** Run events (`nodestro_run_events`, 0.25 s flushes) and their SSE streams are Nodestro's:
  they are high-frequency progress, not notifications.

## Observability (M7)

Nodestro builds no metrics stack of its own: `nodestro_runtime.axentra_metrics.ServerMetrics` registers its metrics
on Axentra's `MetricsRegistry` (Prometheus text format). `GET /api/metrics` exists only with
`NODESTRO_METRICS_TOKEN` (bearer token, constant-time check, `401` without it, not in the OpenAPI document); it is on
the API listener, so the reverse proxy may also restrict the path. It works with `NODESTRO_AXENTRA=off` too (it needs
only the package). Labels come from small fixed sets: never a tenant, user, run or workflow id, never a secret.

| Metric | Meaning |
|---|---|
| `nodestro_db_pool_{size,idle,in_use,max,requests_waiting,requests,requests_errors,connection_errors}{pool}` | Nodestro's and Axentra's pools (`pool="nodestro"`, `"axentra"`); a shared pool appears once |
| `nodestro_integration_operations_total{operation,outcome,detail}` | This process: `mirror_write` (`audit`/`usage`/`run_usage`; `ok`, `failed`, `skipped`, `changed`), `identity_resolve` (`valid`, `none`, `error`), `secret_decrypt` (`v1`/`axs1`; `ok`, `unreadable`), `notification` |
| `nodestro_jobs{kind,status}`, `nodestro_jobs_expired_leases`, `nodestro_workers_active`, `nodestro_jobs_oldest_queued_seconds` | The job queue |
| `nodestro_runs_finished_24h{status}`, `nodestro_run_cost_usd_24h{status}` | Top-level runs finished in the last 24 h and their known cost, all tenants |
| `nodestro_schema_version{runner,version}`, `nodestro_axentra_migrations_pending` | Migration state of both runners |
| `nodestro_axentra_switch{switch}` | Switches of this process (1/0) |
| `nodestro_axentra_mirrored_records{kind}`, `nodestro_axentra_reconcile_backlog{kind}` | Mirrored totals; records `reconcile` would still write (`usage`, `runs`, `audit`) |
| `nodestro_secrets_stored{format}` | Stored tenant secrets by `v1` / `axs1:<key id>` / `plain` |
| `axentra_*` | Axentra's own HTTP metrics (zero here: Axentra's HTTP API is not served) |

Database-derived values are read at most once a minute (the backlog every five minutes) with a 5 s statement
timeout; a failing query keeps the last value. Counters are per API process; the worker has no listener, so its
side shows in the database-derived values (mirrored totals, backlog, jobs).

`nodestro-server axentra status` shows the embedding state for operators: switches, package, both schema runners,
keyring key ids (never keys), stored secrets by format and whether this configuration can read them, the identity
backend and whether it would start, and the reconciliation backlog.

## API keys (M6): decision

The server profile does not serve the published workflow APIs (`create_tenant_app` mounts no
`/api/published` routes; checked by `tests/test_server_obs.py`), so no Nodestro API key (`ndk_`) is checked on a
server and **nothing is migrated now**. The integration boundary for when publication comes to the server:

- **New keys** are Axentra keys (`axk_`, `ApiKeyService`), created in the Axentra tenant linked to the Nodestro tenant
  and linked in `nodestro_axentra_ids` (kind `api_key`) to the key id the Studio shows; hashed by Axentra and shown
  once, like `ndk_` keys today.
- **Existing keys** (`ndk_<id>_<secret>`, SHA-256 of the secret in the `api_keys` documents) stay valid through a
  legacy verifier behind the same `PublishedApi.authenticate` dependency (`nodestro_api.serving`): the prefix picks
  the verifier (`axk_` Axentra, `ndk_` Nodestro), never both; no silent conversion. A key moves only by being
  replaced (a new `axk_` key, the old one revoked), so no caller is cut off by the migration.
- **Tenant binding.** A key belongs to one tenant (Axentra `api_key_tenant_id`) and lists its publications; another
  tenant's or an unlisted publication answers `404 publication.not_found`, as today.
- **Scopes.** Nodestro's `run`, `read`, `approve` become product permissions of product `nodestro`
  (`nodestro.published.run`, ...; Axentra G-1) granted to the key; no generic Axentra permission is given to a key.
- **Rotation.** Replace creates the new key before revoking the old one (both valid until the revocation); revocation
  takes effect on the next request (Axentra checks the key on every request).
- **Error contracts stay Nodestro's:** `401 auth.missing` / `auth.invalid` (with `WWW-Authenticate`), `429
  auth.too_many_failures` and `limit.requests` (with `Retry-After`), `403 auth.scope`, `404 publication.not_found`.
  Axentra's problem types are mapped to these and never exposed.

## What stays Nodestro's

- **Jobs.** Nodestro's job queue keeps its guarantees: one active job per `(tenant, kind, ref)`, the run fence
  (compare-and-set), interruption on lease loss, never a blind retry of model calls or external writes. Axentra's
  jobs are at-least-once with a reaper that re-queues, so they are not a drop-in replacement.
- **Workflow, run, knowledge and memory data**, tool approvals, MCP, execution permissions and the Studio UI.

## Rollback

Every switch can be turned off again without data changes: `NODESTRO_AXENTRA_AUDIT=0`, `..._USAGE=0`,
`..._RUN_USAGE=0`, `..._NOTIFY=0`, `NODESTRO_IDENTITY_BACKEND=mirror` or `nodestro` (see above), `NODESTRO_AXENTRA=off`.

**Secrets (req. 4.5).** `axs1` values can only be read with the keyring, which is only read with
`NODESTRO_AXENTRA=on`. `check`, `api` and `worker` therefore refuse to start (`profile.invalid`, with counts, never
values) when `NODESTRO_AXENTRA=off` while `axs1` values exist, and when the keyring lacks a key id still in use. The
check reads only the few documents that hold secrets and decrypts nothing. The way back:

1. Keep `NODESTRO_AXENTRA=on` with the keyring, set `NODESTRO_SECRETS_FORMAT=v1` and restart (new values are v1).
2. `nodestro-server secrets reencrypt` moves every `axs1` value back to v1 (same guarantees as above; unreadable
   values stay and are reported, exit code 2).
3. When it reports `pending: 0` and `failed: []`, `NODESTRO_AXENTRA=off` starts.

Unreadable values are never overwritten or emptied: re-encryption leaves them as they are, and saving the settings
keeps a provider key that cannot be decrypted byte for byte (it is shown as missing and only replaced when a person
enters a new key); billing admin keys and MCP tokens are only replaced by explicit new values. There is no switch to
start with unreadable secrets: restore the key, or the backup. A full rollback restores the backup taken by
`migrate`.
