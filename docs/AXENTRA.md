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
| `NODESTRO_FILES_BACKEND` | `volume` | `axentra`: the bytes of input files are stored through Axentra's storage (local directory or S3). Needs `on`; see [Files](#files-m8) |
| `NODESTRO_FILES_SOURCE_REMOVAL` | `0` | `1` allows `nodestro-server files remove-source` to delete migrated volume files |
| `NODESTRO_FILES_SOURCE_RETENTION_DAYS` | `30` | Days a migrated volume file stays as the fallback before `remove-source` may delete it |

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
| `axentra` | Axentra: session validity (absolute/idle expiry, revocation, disabled user; database clock), roles **and their permissions**, memberships, member lists, tenant list and status; Axentra also runs the OIDC login | both, one transaction (Nodestro's tables stay current for the rollback) |

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
  `viewer`, `editor`/`operator` -> custom tenant roles of the same key in product `nodestro` (generic tenant
  permissions, no `members:manage`, no tenant settings, plus Nodestro's own, see below). `/api/auth/me` is unchanged.
- Sessions: the same token. Axentra's `token_hash` is the hex form of Nodestro's `bytea` SHA-256, so the hash joins
  both rows (no mapping row). New sessions are started by Axentra (`start_session` makes the token, Nodestro stores
  its hash); both rows get Nodestro's expiry. The active tenant stays `nodestro_sessions.tenant_id`. `last_seen_at`
  touches, logout, "log out everywhere", removal of a member (ends their sessions in that tenant at once) and
  housekeeping are mirrored. A tenant switch still issues a new session and ends the old one.
- Bootstrap tokens (setup links) stay Nodestro's in every mode (decision on plan gap G-5: they must work in every mode
  and after a rollback, so there is one store); using one writes the owner membership on both sides. Login states:
  Nodestro's table in `nodestro`/`mirror`, Axentra's in `axentra` (10 minutes either way; a login started across a
  restart into another mode fails once with `identity.login_expired`).

**Permissions and roles (report risk 10, decided).** Nodestro's permissions are product permissions of product
`nodestro`, tenant scope: `nodestro.workspace:read`, `:edit`, `:run`, `:admin` (`install` is never a tenant
permission). `tenant_owner` and `tenant_admin` hold all four by Axentra's rules, `viewer` holds `read`
(`default_roles`), and the custom roles list theirs (`editor`: read, edit, run; `operator`: read, run): the same table
as `ROLE_GRANTS` / the API's `ROLE_PERMISSIONS` (an import-time assertion and a test keep them equal). They are
registered as **versioned access definitions** (`access_definitions()`, `ACCESS_REVISION`; Axentra's
`ax.access_definitions.apply`) at every start of `api`, `worker` and the identity commands in `mirror`/`axentra`:

- the same revision is a no-op; a newer one is applied once, under an advisory lock, audited
  (`access.definitions.apply`); an older process (rolling deployment) never downgrades it (`stale`, logged); the same
  revision with other content stops the start (`identity.access_conflict`). A test pins the content hash of every
  revision, so a change without a new revision fails CI. "Last start wins" is gone.
- The managed roles `editor`/`operator` cannot be changed over Axentra's API (`409`); a later revision changes them.
- **Source of permission checks.** In `axentra` mode the session carries the permissions Axentra's role definitions
  grant (read in the same request as the session: a role change, a removal or a newer revision applies to the next
  request in every process). In `nodestro` and `mirror` mode `ROLE_PERMISSIONS` decides. Route checks are unchanged
  (`read/edit/run/admin`). Reason: one identity service answers in `axentra` mode, and the permissions come from the
  registered definitions, so every process agrees even during a rolling deployment.

**Suspended tenants (report risk 7, decided).** Nodestro tenants have a status (`active`/`suspended`, migration
`0008_tenant_status`); `nodestro-server suspend-tenant ID` / `resume-tenant ID` set it (on both sides with Axentra
identity). A suspended tenant is closed, not deleted:

- its sessions are refused with `403 identity.tenant_suspended` (every route, event streams end at their next
  check); a login to it, a tenant switch to it and its setup links are refused with the same code (the links stay
  unused); `/api/auth/me` lists only active tenants; a login without a tenant goes to an active one;
- workers fail a claimed job of it with `identity.tenant_suspended` before its run starts or continues (no model
  call; the orphan sweep later marks the run interrupted, so it can be continued after resuming), stop runs they
  execute for it within `reap_every` without storing more (as after a lost lease), and fire no schedules and no
  tool-wait sweeps for it. The check is `IdentityStore.tenant_active`, answered by the identity backend.
- In `axentra` mode a tenant counts as suspended when **either** side says so: a suspension made directly in Axentra
  closes it at once (fail closed), and resuming it needs Nodestro's command when Nodestro's side is suspended. In
  `nodestro`/`mirror` mode Nodestro's status decides (`mirror` writes it to Axentra too). The backfill copies a
  suspension to Axentra and never resumes an Axentra-side one (it reports `suspended_in_axentra_only`); the
  reconciliation reports a status that differs.

**Login (M5 OIDC, decided: Axentra in `axentra` mode).** Axentra's `OidcService` meets Nodestro's requirements:
authorization code flow with PKCE `S256`, random `state` consumed exactly once before any check (stored hashed),
`nonce`, the browser binding (login cookie = state, no login CSRF), exact issuer, audience/`azp`, asymmetric
algorithms only, key rotation with a rate-limited refresh, response size limit, and it never links by e-mail with
`link_by_verified_email=false`. Generic additions made for this (Axentra `0009`): the host's callback URL
(`redirect_uri=`), host context carried through the login (the requested tenant and the bootstrap token's hash,
encrypted), and `finish(start_session=False)` with the verified identity. So in `axentra` mode `/api/auth/login` and
`/api/auth/callback` run on Axentra (`nodestro_api.identity.AxentraLoginFlow`): Axentra keeps the login state,
verifies the provider, finds or creates the user of `(issuer, subject)`, refuses a disabled account
(`identity.account_disabled`), rate-limits starts per client address (`identity.rate_limited`, 429; behind a proxy
set `FORWARDED_ALLOW_IPS` so uvicorn sees client addresses, or raise `security.login_rate_per_minute`) and audits
refused logins; Nodestro then writes its user row, checks that Nodestro's link names the same Axentra user, applies a
setup link and starts the session in the chosen tenant (both sides). Routes, cookies, error codes and redirects are
unchanged; Axentra's codes map to Nodestro's (`login_expired` -> `identity.login_expired`, `provider_unavailable` ->
`identity.provider`, everything else -> `identity.login_failed`). In `nodestro`/`mirror` mode Nodestro's own client
runs the login (it must work without Axentra). Session rotation in every mode: a login ends the session the browser
held before, and a tenant switch issues a new session and ends the old one. Logout, "log out everywhere"
(`revoke-sessions`) and removal end sessions on both sides; ended sessions stay ended across every mode switch.
One provider per server (`NODESTRO_OIDC_*`); a person is an identity `(issuer, subject)`, so changing the provider or
a second issuer with the same verified address gives a new person without access (never merged by e-mail).

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
matters for idle expiry after a rollback); the permissions of `axentra` mode add one short read (the product's
permission catalog). In `axentra` mode the tenant list, roles, permissions and members come from Axentra, the user
profile (`/api/auth/me` name and address) from Nodestro's row. Axentra's own HTTP API and admin functions are not
exposed through Nodestro.

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

## Files (M8)

`NODESTRO_FILES_BACKEND=axentra` stores the bytes of **input files** (run dialog, test cases, schedules, batches,
published APIs) through Axentra's `StorageService` (`nodestro_runtime.axentra_storage`). The metadata stays Nodestro's
document (`input_files`, ids `in_<12 hex>`), the blob id stays the first 16 hex digits of the SHA-256, so every file
reference, run, test case and API response is unchanged. Agent delivery (`file_delivery.py`), the studio and the REST
API read through `InputFiles` in the API and the worker processes alike.

**Where the bytes go.** Axentra's own `[storage]` settings in `NODESTRO_AXENTRA_CONFIG` choose the backend:

```toml
[storage]
backend = "local"                       # or "s3"
local_path = "/var/lib/nodestro-files"  # absolute; shared by API and workers (check refuses a relative path)
# s3_bucket = "nodestro-files"  s3_region = "eu-central-1"  s3_endpoint_url = "https://s3.example"  s3_prefix = "nodestro"
# s3_access_key_ref = "env:AWS_ACCESS_KEY_ID"  s3_secret_key_ref = "env:AWS_SECRET_ACCESS_KEY"  (or the AWS default chain)
max_file_bytes = 52428800               # Axentra's default (50 MB) equals Nodestro's upload limit
# default_quota_bytes = ...  default_quota_files = ...   per-tenant quotas
```

Objects are keyed `<product>/<axentra tenant>/<file>`; the Axentra tenant is the linked one (`nodestro_axentra_ids`,
created on first use). The mapping blob -> Axentra file is one document per blob in the tenant's collection
`file_objects` (tenant-isolated like every Nodestro document; no schema migration). Identical content of a tenant is
stored once.

**What Axentra adds.**

- Size limit and **quotas** per tenant (bytes and files; an upload and its cached extraction count as two files). A
  refused upload is reported like any refused file (`input.too_large`, `input.quota_exceeded`); nothing is stored.
  When only the cached extraction does not fit, the file is kept and its text is read again on use.
- **Content-type policy (G-7).** Executable, HTML, SVG and script files are *blocked* types in Axentra. Nodestro has
  always accepted code and markup files as inputs (agents read them as text), so at start Nodestro sets product
  `nodestro` to `store_inert` unless the product already has a policy of its own. Such files are stored as opaque
  `application/octet-stream` objects, and `GET /api/inputs/{id}/content` serves them as `application/octet-stream`
  (always `attachment`, `nosniff`, sandboxing CSP, as before). An operator who sets the product to `refuse` gets
  `input.blocked_type` for them.
- **Integrity.** Axentra hashes every object while streaming it; Nodestro compares the SHA-256 on every read and
  never serves a changed object (`input.integrity`; a missing object is `input.content_missing`).
- An audit event per upload and delete (`storage.file.*`, actor `system`/`nodestro.files`).

**Not included.** Knowledge files: on a server they already live in PostgreSQL (`nodestro_files` and `nodestro_chunks`, with
the search index, in one transaction, covered by `pg_dump`); moving them would split the bytes from their index without
a gain. Result files are documents. Axentra's HTTP routes and signed download URLs are not exposed: downloads stay on
Nodestro's routes. Desktop and Local Docker never use any of this (they never import Axentra).

**What is still shared.** With the S3 backend API and workers share nothing on disk for files uploaded after the
switch. With the local backend `local_path` must be one directory shared by API and workers (as the data volume
was). Until `files remove-source` ran, files from before the switch (and files imported from a desktop with
`postgres_import`) are read from the data volume, so keep it shared until then. Tools and Python environments stay
on the data volume.

**Procedure.**

1. Back up (`nodestro-server backup DIR`). Set `[storage]` in `NODESTRO_AXENTRA_CONFIG` and
   `NODESTRO_FILES_BACKEND=axentra`; `nodestro-server check` must print `ready`. Restart `api` and `worker`: new uploads
   go to Axentra; older files are read from the volume (fallback).
2. `nodestro-server files migrate --dry-run [--tenant ID]` counts the volume blobs to copy; `files migrate` copies
   them (resumable, idempotent; each copy is read back and its SHA-256 compared before the mapping is recorded; a
   damaged volume file is reported, not copied; the volume file is **never** deleted). Exit code 2 when a file failed.
3. `nodestro-server files verify` reads every object back, compares the SHA-256 and records `verified_at`; it reports
   failures, volume blobs not migrated yet (`pending`) and Axentra objects without a mapping (`orphans`, e.g. from an
   interrupted upload; they are reported, never deleted automatically).
4. Later, after the fallback period: `NODESTRO_FILES_SOURCE_REMOVAL=1 nodestro-server files remove-source --dry-run`,
   then without `--dry-run`. A volume file is deleted only when its object was migrated, verified by `files verify`,
   the fallback period (`NODESTRO_FILES_SOURCE_RETENTION_DAYS`, default 30) since its migration has passed and a fresh
   read still matches; otherwise it is kept (`kept`) or reported (`failed`).

`files migrate` needs `NODESTRO_FILES_BACKEND=axentra`: while the volume backend runs, a deleted file would only be
deleted on the volume and its copy would stay in Axentra.

**Deletes.** Deleting an input file (or the cleanup of unused files) removes every copy: the Axentra object, the
mapping and a volume copy. Concurrent migration never resurrects a file deleted while it was copied (both take the
blob's lock).

**Backup and restore.** The database dump contains the input file documents, the mappings and Axentra's file rows.
With the local backend `nodestro-server backup DIR` also writes `nodestro-files-<time>.tar.gz` of `local_path`
(uploads in progress left out). With S3, back up the bucket (versioning or replication); objects are immutable, a
delete removes them. Restore the dump and the archive (or bucket) of the same time, then `files verify` must print
`"ok": true` (a restore drill does exactly this in `nodestro-api/tests/test_server_files.py`). The desktop backup
(`nodestro_api.backup`) is unchanged: the desktop never uses Axentra storage.

**Rollback.** `nodestro-server files export-volume [--dry-run]` copies every file that exists only in Axentra back to
the volume (idempotent); then `NODESTRO_FILES_BACKEND=volume` and restart. Mappings and objects stay; switching
forward again later reuses them (`files migrate` copies only what is new).

**Cost (measured, one machine, PostgreSQL 16 local, medians).** Per blob operation (put / get / delete, ms):

| Backend | 10 KB | 1 MB | 20 MB |
|---|---|---|---|
| volume | 0.4 / 0.1 / 0.3 | 2.7 / 0.3 / 1.5 | 35 / 12 / 2.2 |
| Axentra local | 11.6 / 1.7 / 8.8 | 15.7 / 3.8 / 10.4 | 112 / 53 / 26 |
| Axentra S3 (moto server, local) | 26 / 11.5 / 25 | 32 / 14 / 23 | 432 / 92 / 30 |

Axentra adds a fixed ~10 ms per upload (pending row, quota row lock, activation and audit in their own transactions,
`fsync`) and a few ms per read (authorization, row lookup, SHA-256 check). An upload stores two objects (the file and
its extraction). The moto numbers show the protocol overhead only, not a real S3 latency.

## What stays Nodestro's

- **Jobs: M11 closed by architecture decision (variant B, final).** Run execution stays on Nodestro's own queue
  (`nodestro_runtime.jobs`, `nodestro_api.worker`, tables `nodestro_jobs`, `nodestro_run_events`,
  `nodestro_schedule_firings`) permanently; there is no `AxentraQueue` adapter and no switch for it. Axentra's jobs,
  events and schedules remain the infrastructure for generic work of the platform, never for work that calls a
  model or a tool. Variant A (Axentra gets configurable at-most-once semantics that fully cover Nodestro) was
  evaluated and rejected:
  - *The guarantee is not a queue property.* "Never pay twice, never write outside twice" comes from the queue
    **together with** the run fence (a compare-and-set on the run document in `nodestro_documents`), the
    `run.interrupted` state and its sub-workflow cascade, the tool journal and the continue confirmation
    (`resume.plan`), the tool-approval checkpoints and a worker capacity model in which waiting runs keep their slot
    (`WaitingSlots`). Moving only the queue would leave all of these in Nodestro and add a callback from Axentra's
    reaper (interrupt the run) and into its claim loop (capacity), across two transactions and two schemas.
  - *What Axentra would have to add is Nodestro's semantics, not a generic feature.* "No retry" is already possible
    there (`max_attempts=1`: a lost lease makes the job `dead`); what is missing is a unique *active* key per piece
    of work (Axentra's idempotency keys deduplicate for good), an `interrupted` outcome with a domain callback, a
    claim gate fed by the product's own capacity, and asyncio-held runs instead of one thread per job. Only the
    first is generally useful, and no other Axentra product needs it today.
  - *Tenancy.* Axentra jobs are scoped by Axentra product and tenant with row-level security; with
    `NODESTRO_IDENTITY_BACKEND=nodestro` (the default) or a 3.10 server without the package there is no Axentra
    tenant to scope a run to, and the queue must work there.
  - *Cost and risk without gain.* M11 would have meant draining the queue at the switch, rewriting the worker and
    re-proving every property below, for one table less. Axentra's default semantics, used naively, would have
    re-run a handler after a lease loss while the old worker was still running it (proven by axentra
    `tests/integration/test_job_delivery_semantics.py`).

  The evidence is a set of tests on real PostgreSQL: `tests/test_job_delivery.py` (a worker killed with SIGKILL
  during a model call; a stalled worker whose run was interrupted makes no further model or tool call; an external
  write before a lost lease is repeated only after a person confirms; four parallel workers execute twelve runs
  exactly once each; cancel and resume reuse finished steps; a nested sub-workflow of a worker that lost its lease
  stores and calls nothing more), together with `test_jobs.py` (one active job, `SKIP LOCKED`, owner-only renewal,
  reaped once, schedule slots, leader lock), `test_run_fencing.py`, `test_worker_approvals.py` (capacity slots, a
  killed worker while a question waits, load), `test_worker_checkpoints.py` (paused calls resumed exactly once) and
  `test_resume_side_effects.py`. Since this decision a queue execution also asks the queue whether its worker still
  holds the job before every model request and every tool call (`RunManager.owns`, `RunWorker.owns`), so a worker
  with a stalled heartbeat stops before it acts, not only at its next renewal.
- **Workflow, run, knowledge and memory data**, tool approvals, MCP, execution permissions and the Studio UI.

## Rollback

Every switch can be turned off again without data changes: `NODESTRO_FILES_BACKEND=volume` (after
`files export-volume`, see [Files](#files-m8)), `NODESTRO_AXENTRA_AUDIT=0`, `..._USAGE=0`,
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
