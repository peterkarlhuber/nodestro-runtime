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
| `NODESTRO_AXENTRA_ALLOW_SUPERUSER` | `0` | `1` lets Axentra run on a superuser, `BYPASSRLS` or table-owning role (development only). Its row-level security is then no defense in depth. Refused otherwise in every environment: see [Database roles](#database-roles) |
| `NODESTRO_DATABASE_MIGRATION_URL` / `_FILE` | – | The owner role's connection: `migrate`, the backup and `db grant-runtime-role` use it; `NODESTRO_DATABASE_URL` is then the runtime role. Without it every command uses `NODESTRO_DATABASE_URL` (one role, as before) |
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
advisory lock), as the owner role ([Database roles](#database-roles)). One backup (`--backup-dir` runs `pg_dump`, `--backup-taken` states one was made) covers both runners;
a new, empty database needs none. `check`, `api` and `worker` refuse to start while either schema is behind
(`storage.schema_mismatch`). Switching Axentra off again needs nothing: its tables stay and are not read.

Nodestro migration `0007_axentra_links` adds `nodestro_axentra_ids(kind, nodestro_id, axentra_id)`: the only
mapping between Nodestro ids and Axentra ids (`tenant`, `user`, `session`, `api_key`), unique in both directions.
Nodestro ids never change.

One connection pool per process: Axentra borrows the pool of Nodestro's `PostgresDatabase`
(`axentra.data.Database.on_pool`, see [One connection pool](#one-connection-pool-m10)); `AxentraServices.start`
refuses to run on a pool of its own.

## Database roles

Row-level security on Axentra's tables protects only a role that is not a superuser, has no `BYPASSRLS` and **owns
no table** (`FORCE ROW LEVEL SECURITY` binds an owner too, but an owner can switch it off, change policies or drop the
table). A server therefore uses two roles:

| Role | Used by | Attributes and privileges |
|---|---|---|
| owner (for example `nodestro_owner`) | `migrate`, `backup` (and the pre-migration `pg_dump`), `db grant-runtime-role`, `db roles` via `NODESTRO_DATABASE_MIGRATION_URL` | owns the schema and every table; `NOSUPERUSER`, `BYPASSRLS` (so that `pg_dump` can read Axentra's forced-RLS tables; it gains nothing an owner could not take anyway) |
| runtime (for example `nodestro_app`) | `api`, `worker`, `check` and the operator commands via `NODESTRO_DATABASE_URL` | `NOSUPERUSER NOBYPASSRLS`, owns nothing; `SELECT, INSERT, UPDATE, DELETE` on the tables, sequences, functions, read-only on `nodestro_schema_migrations` and `axentra_schema_migrations` |

- `nodestro-server migrate` checks the runtime role first (refused, nothing migrated: superuser, `BYPASSRLS`, owns
  tables) and grants it afterwards (`grant_runtime_role`, idempotent; default privileges cover later migrations).
  `nodestro-server db grant-runtime-role [ROLE]` does the same on its own, `db roles` shows both roles (exit code 3
  when RLS does not fully apply to the runtime role).
- `check`, `api` and `worker` refuse a runtime role that bypasses RLS or owns tables (`storage.role_unsafe`) when
  `NODESTRO_AXENTRA=on` (unless `NODESTRO_AXENTRA_ALLOW_SUPERUSER=1`) or a separate migration role is configured;
  `AxentraServices.start` checks the same in every environment before writing anything.
- Proof: `tests/test_database_roles.py` (runtime: cannot `ALTER ... NO FORCE / DISABLE ROW LEVEL SECURITY`, change
  policies, alter, drop or truncate tables, create objects, fake schema versions, `SET ROLE` the owner or give itself
  `BYPASSRLS`; sees no Axentra row without a scope, cannot write into another tenant; `row_security = off` fails
  instead of bypassing) and nodestro-api `tests/test_server_roles.py` (migrate as owner, serve as runtime, the backup,
  refusals). `NODESTRO_TEST_RUNTIME_ROLE=1` runs every PostgreSQL test of this repository with migrations as the
  owner and everything else as such a runtime role (`tests/postgres_support.py`).
- Found that way: Axentra's audit/usage mirror did not work under a non-superuser role (an Axentra transaction inside
  `Database.joined` emptied the scope of the enclosing `join`, so the meter was invisible); fixed in Axentra, see its
  CHANGELOG (included in the current pin `f6ae01d`).

## One connection pool (M10)

`AxentraServices(database=...)` makes Axentra borrow the `PostgresDatabase` pool (`Database.on_pool`); `api`,
`worker`, `check` and the operator commands always pass it, so a process has one budget, `NODESTRO_DATABASE_POOL_SIZE`
(default 8), plus Nodestro's one connection for session advisory locks. Only `migrate` and `axentra status` (no pool
needed) open nothing but the migration runners' own connections.

- Axentra's scope settings are transaction-local (`set_config(..., true)`); each Axentra transaction is an explicit
  block on the autocommit connection. A connection handed back to Nodestro carries no scope, after a commit, a
  rollback or an error; `joined`/`join` nest and restore the scope that was in effect
  (`tests/test_database_roles.py`: pool of one connection, the same backend alternating tenants, Nodestro and
  Axentra; six threads on two connections).
- Measured with `tests/bench_shared_pool.py` (local PostgreSQL 16, 8 s per mode; API loop = an Axentra service call,
  a Nodestro read, a Nodestro write, a joined audit mirror; worker loop = a Nodestro transaction with a lock):

  | Load (API threads / worker threads / pool) | Connections max (mean), separate pools | Connections max (mean), shared | ops/s separate / shared | API loop p50 ms separate / shared |
  |---|---|---|---|---|
  | 4 / 1 / 8 | 8 (6.7) | 4 (4.0) | 190 / 204 | 27.3 / 24.5 |
  | 16 / 2 / 8 | 9 (8.9) | 6 (6.0) | 139 / 138 | 126 / 125 |
  | 16 / 2 / 4 | 9 (9.0) | 4 (4.0) | 160 / 163 | 105 / 106 |
  | 40 / 2 / 8 | 12 (11.9) | 7 (7.0) | 118 / 118 | 339 / 337 |

  No errors and no pool timeouts in either mode; no thread ever held two pooled connections at once (no
  hold-and-wait, so one pool cannot starve itself). Throughput is bounded by Nodestro's per-process write
  serialization, not by connections. Before, the worst case per process was 8 + 10 (Axentra's `pool_max`) + 1.

## Migrations (M9: decided, Nodestro keeps its runner)

Nodestro's PostgreSQL migrations stay on Nodestro's runner, now with checksums: each applied migration is recorded
with the SHA-256 of its file; `migrate`, `check`, `api` and `worker` refuse a shipped migration whose file differs
from the one applied (`storage.migration_changed`); rows of earlier releases are adopted once (the file name must
match; the shipped files never changed since 0001, checked in git history). Axentra's `MigrationRunner` was not
adopted as the runner of the `nodestro` namespace because:

- the runner must work without Axentra: Python 3.10, `NODESTRO_AXENTRA=off` (no `axentra_*` tables may be created),
  and a server that switches Axentra off again. Running Axentra's runner only when Axentra is on would mean two
  bookkeeping tables for the same schema and a reverse adoption on every switch back;
- Nodestro applies all pending steps in **one** transaction (a failure leaves the database unchanged); Axentra
  commits per step;
- Nodestro's schema versions are a gap-free sequence that other work packages extend in parallel; the gate
  (`storage.schema_mismatch`, `storage.schema_too_new`) is exact, as `require_current` would be.

What Axentra's runner adds was taken over where it matters (checksums and drift detection); its advisory lock,
before-apply backup and forward-only rules Nodestro already had. One backup still covers both runners; both run as
the owner role. Desktop and Local Docker use SQLite (no PostgreSQL migrations).

## Row-level security on Nodestro's tables (M10: decided, not enabled)

Measured with `tests/bench_nodestro_rls.py` (20,000 documents of 20 tenants, runtime role, local socket; p50 in µs):

| Statement | today (autocommit) | FORCE RLS, `BEGIN` + `set_config` + statement + `COMMIT` | same, one pipeline | policy cost alone |
|---|---|---|---|---|
| point read | 85 | 236 | 211 | 57 (inside a transaction) |
| 1,000-row listing | 1,997 | 1,721 | 1,769 | 1,549 |
| upsert | 327 | 555 | 654 | 70 (no commit) |

The policy itself is cheap; the cost is that every read and single write must become a transaction to carry a
transaction-local tenant setting (three more round trips per statement, about 2.5x on a local socket and
milliseconds over a network; a pipeline saves the round trips but not the commit). Not enabled, because:

- the gain is small: the setting would come from the same store object that already puts `tenant_id` into every
  statement, so it catches only a forgotten filter, not a wrong store; and any role can call `set_config`, so it
  does not stop SQL injection either;
- many paths are cross-tenant by design and would run in a bypass scope anyway: the worker's claim, lease renewal and
  finish by job id, the reaper, housekeeping, the schedule leader, session lookup by token hash before a tenant is
  known, the global user table;
- it would change the store's connection model (autocommit reads with retry on a lost connection) in every module
  that writes SQL (storage, knowledge, jobs, identity), which other work packages change at the same time.

Instead, a static check keeps every statement bound to one tenant (security review SR-01 found the first version
unsound: a substring test for `tenant_id` on five tables of the runtime only). `nodestro_runtime/tenant_sql_check.py`,
run by `tests/test_tenant_filters.py` of the runtime and of the API:

- tenant tables are every `nodestro_*` table with a `tenant_id` column in the migrations (read at test time, so a new
  table is covered at once): documents, files, chunks, memory files, jobs, run events, schedule firings, sessions,
  memberships, login states, bootstrap tokens (runs, schedules and secrets are collections of the documents table);
- every string expression of `nodestro_runtime` and `nodestro_api` is rebuilt as Python would build it (string and
  bytes constants, `+` and implicit concatenation, f-strings, `str.format`, `join` of a literal list or of a name
  holding one, psycopg `sql.SQL`/`Identifier`/`Placeholder`/`Composed`; `.replace` and other methods that change the
  text add an unknown part), and every variant that names a tenant table is parsed with PostgreSQL's own grammar
  (`pglast`, a dev dependency);
- a name is resolved to every value it can hold at each place it is read, inside a string expression and on its own
  (`query += " OR ..."; db._query(query)`, `query = query + ...`): module constants, the local assignments and `+=`
  before the read (and after it in a loop around it), each conditional assignment or `+=` taken and not taken, and
  loops over constant tuples; a `+=` that a loop repeats is an unknown part, and more than 256 variants are reported
  as dynamic (SR-01a);
- each reference to a tenant table (`FROM`, joins, `USING`, `UPDATE`/`DELETE` targets, subqueries, CTEs, set
  operations) must be bound by a top-level `AND` conjunct `tenant_id = <param>` or `tenant_id = ANY(<param>)` (or
  equal to the `tenant_id` of a bound or enclosing reference); `INSERT` must list `tenant_id`; `TRUNCATE`, `COPY`,
  `MERGE` are refused;
- a statement with a part that cannot be resolved is reported as dynamic, one the parser rejects as unparsed; neither
  passes silently. A text that starts with an unknown part (an imported fragment, `self.BASE + "..."`, a function's
  result) followed by SQL words is reported as dynamic too, not dropped (SR-01b);
- the exceptions are in `tenant_sql_allowlist.py` of each package: the module and the whole normalized statement,
  each with its reason (worker claim, heartbeat and finish by job id, the reaper, housekeeping, session lookup by
  token hash, operator commands, metrics aggregates). Matching is exact, and an entry that matches nothing fails.
  The desktop's SQLite modules are skipped by name, and the check fails if one of them uses PostgreSQL.

What it cannot see: SQL text that comes from outside the source (none exists), and a statement whose table name and
leading keyword both arrive at run time from a value the check cannot resolve (it would be reported only if it starts
with a data keyword or completes a `nodestro_` name). The runtime role cannot alter the tables either. Revisit RLS when
Nodestro's store runs inside per-request transactions anyway.

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

The start (`check`, `api`, `worker`) refuses a keyring that lacks a key id still in use, in Nodestro's documents or in
Axentra's own keyring columns (login states, webhook secrets, notification messages, the inbox), and it decrypts up to
five stored values per key id and column, in both places (`secret_key_problems`, `AxentraServices.key_samples`):
other bytes under a known key id are refused, also for a key id only Axentra's columns use (security review SR-04,
SR-04a). Problems name locations, never values.

The same procedure moves v1 values to the keyring (`NODESTRO_SECRETS_FORMAT=axs1`). Until every v1 value is
re-encrypted, `NODESTRO_SECRET_KEY` must stay set.

## Tenants

`AxentraLink.ensure_tenant` creates the Axentra tenant of a Nodestro tenant (slug derived from the id, `-2`, `-3` on a
collision, `settings.nodestro_tenant` = the Nodestro id) in the caller's Nodestro transaction, serialized by an
advisory lock; `nodestro-server axentra backfill-identity` links every existing tenant (idempotent, resumable). Pseudo tenants without a
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
- API processes stop what its context started in the background within `suspension_check` seconds
  (`server_app.stop_tenant_work`, stop code `identity.tenant_suspended`): batches, benchmarks with their judge,
  assistant answers, architect drafts, live run chat answers (`RunChat.cancel_answers`: the question fails at once and
  a late answer is not stored) and Python environment installations (`PythonEnvironments.cancel_installs`: stopped
  before their next step; a `uv` or `docker build` command already running finishes, bounded by its timeout, but its
  result is never checked or used). Security review SR-03, SR-03a.
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

**Limits.** Session resolution in `axentra` mode is two Axentra transactions (Axentra's `resolve_session`, then
Nodestro's membership/tenant read with the product's permission catalog) instead of one statement: about 1.3-1.9 ms
instead of 0.2-0.4 ms, see [Performance](#performance); when Axentra notes a use on its own (`resolve_session`, at
most once a minute) Nodestro's `last_seen_at` may lag up to about two minutes (it only matters for idle expiry after
a rollback). In `axentra` mode the tenant list, roles, permissions and members come from Axentra, the user
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
| `nodestro_db_pool_{size,idle,in_use,max,requests_waiting,requests,requests_errors,connection_errors}{pool}` | The process's pool (`pool="nodestro"`); Axentra borrows it, so it has no series of its own |
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
   failures, volume blobs not migrated yet (`pending`) and leftovers older than an hour: Axentra objects without a
   mapping (`orphans`, e.g. from an interrupted upload or copy) and mappings no input file uses (`unreferenced`, e.g.
   a file deleted while the backend was `volume`). A leftover makes the report `"ok": false` (exit code 2);
   `nodestro-server files cleanup --dry-run [--tenant ID]` lists them, `files cleanup` deletes them in Axentra
   (volume files are never touched; younger leftovers are only counted as `recent`).
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
forward again later reuses them (`files migrate` copies only what is new). Deletes made while the backend was
`volume` reach only the volume: after switching forward again run `files verify` and `files cleanup`, which delete
the Axentra objects of those files.

**Cost (measured, one machine, PostgreSQL 16 local, medians).** Per blob operation (put / get / delete, ms):

| Backend | 10 KB | 1 MB | 20 MB |
|---|---|---|---|
| volume | 0.4 / 0.1 / 0.3 | 2.7 / 0.3 / 1.5 | 35 / 12 / 2.2 |
| Axentra local | 11.6 / 1.7 / 8.8 | 15.7 / 3.8 / 10.4 | 112 / 53 / 26 |
| Axentra S3 (moto server, local) | 26 / 11.5 / 25 | 32 / 14 / 23 | 432 / 92 / 30 |

Axentra adds a fixed ~10 ms per upload (pending row, quota row lock, activation and audit in their own transactions,
`fsync`) and a few ms per read (authorization, row lookup, SHA-256 check). An upload stores two objects (the file and
its extraction). The moto numbers show the protocol overhead only, not a real S3 latency.

## Performance

Measured on one machine (4 CPUs, local PostgreSQL 16, Python 3.13) with the scripts `nodestro-api/tests/bench_*.py`
(run each with `python tests/bench_x.py`; shared set-up in `tests/perf_support.py`; raw JSON and a summary in the
project's `axentra-migration/performance/` folder). *before*: every switch off, one role. *after*: `on`, identity
`mirror`/`axentra`, audit, usage, run usage, inbox, files on Axentra (local), `axs1` secrets, runtime role + owner
role. 50 tenants, 2,000 users, 3,537 memberships, 10,000 sessions unless noted.

| What | before | mirror | axentra |
|---|---|---|---|
| Session resolution, sequential p50 / p99 (ms) | 0.43 / 1.7 | 0.34 / 3.4 | 1.93 / 6.9 |
| ... same session again within the minute (no touch) p50 | 0.16 | 0.15 | 1.25 |
| ... resolutions/s, 1 / 8 / 32 threads, pool 8 | 2,560 / 2,860 / 3,210 | 1,070 / 1,040 / 1,070 | 390 / 394 / 391 |
| `GET /api/auth/me` p50 (ms, real API process) | 2.7 | 2.5 | 4.2 |
| `GET /api/workflows` / upload 10 KB / download p50 (ms) | 4.5 / 4.6 / 3.4 | 4.5 / 5.3 / 5.7 | 6.0 / 6.6 / 6.5 |
| `POST /api/auth/switch` p50 (ms) | 4.2 | 11.7 | 11.2 |
| API mix, 8 clients, pool 8: requests/s, p95 (ms) | 340, 31 | 266, 69 | 135, 87 |
| Audit event / usage entry write p50 (ms) | 0.63 / 1.0 | | 2.3 / 17.4 |
| API process RSS after start / after load (MiB) | 105 / 124 | 118 / 137 | 115 / 132 |
| Worker process RSS after start / after load (MiB) | 91 / 141 | | 104 / 147 |

- **Fixed while measuring.** (1) The identity reads of `axentra` mode joined `nodestro_axentra_ids` (collation `"C"`)
  to Axentra's id columns (database default): PostgreSQL could not use Axentra's indexes and read every membership,
  so `/api/auth/me` took 22 ms at 3,537 memberships and grew with all tenants' memberships; now 4.2 ms (the reads
  filter on Axentra's ids, `tests/test_axentra_identity_indexes.py`). (2) The worker's periodic run usage pass read
  the mirrored events of every run ever settled (0.25 s at 2,000 runs, 0.70 s at 10,000, growing); now it reads only
  its candidates (0.04 s, flat; `tests/test_axentra_run_usage_reads.py`). (3) Axentra resolves a session in one
  transaction instead of two (Axentra commit `69c06a5`, needs a pin bump): with (1), sequential resolution 2.8 -> 1.9
  ms and 270 -> 390 resolutions/s.
- **Accepted: the cost of `axentra` mode per request (~1.5-2 ms CPU).** Axentra answers (validity, revocation,
  disabled users, roles, permissions from the versioned definitions) through its own service API, one transaction
  each; a single combined statement would duplicate Axentra's session rules in Nodestro. The process is CPU-bound
  (the GIL), not pool-bound: under load an API process serves about 40 % of the requests per second of `before`
  (110-137 vs 300-330 requests/s with 16 clients and a worker load; pool 16 removes the pool waits in both and
  changes little). Scale with more API processes; the connection budget per process stays `NODESTRO_DATABASE_POOL_SIZE`.
- **Accepted: the usage mirror (+16 ms per usage entry, 4 meters).** Axentra's `UsageService.record_in` runs about 15
  statements per meter (authorization, meter, idempotency, counters, quota) inside the caller's transaction, so the
  process's write lock is held that long. Usage entries are written once per AI operation (seconds of model time);
  run usage is settled by the leader in the background (35 runs/s). A batch `record_in` is an Axentra follow-up.
  Audit mirror: +1.7 ms. A failing mirror costs 2.4 ms and keeps Nodestro's record.
- **Mirror mode** answers from Nodestro's tables (same latency as before); its cost is in writes: a session touch
  (once a minute per session) and a tenant switch (11.7 vs 4.2 ms) are written on both sides.
- **Connections: no new bottleneck.** One pool per process; backends per server equal for both configurations at
  every pool size (4/8/16: max 12-13 / 15-16 / 23); no pool timeouts, no errors; worker pools at a mean of 0.1-0.6
  connections; no thread holds two connections at once (`tests/bench_shared_pool.py`).
- **Workers are unchanged.** 1/2/4 workers x 200 runs (fake model 200 ms, one executing run per worker): 4.1/7.5/12.9
  runs/s before, 3.4/7.6/12.9 after; job time p50 233 vs 234 ms. A worker killed with SIGKILL under load (3 workers,
  200 runs): its one running run ends `run.interrupted` after the lease, the other 199 succeed, 200 model calls, none
  twice, in both configurations.
- **Migration commands** (killed with SIGKILL at about 40 % and run again: all counts equal, no duplicates, the reruns
  find nothing to do): `backfill-identity` 24.8 s (15,587 records, 630/s; rerun 13 s), `reconcile-identity` 0.12 s,
  `secrets reencrypt` 5,000 documents 6.3 s, `files migrate` 4,000 blobs (16 MB) 44 s, `files verify` 13 s,
  `axentra reconcile` 2,000 audit events + 2,000 usage entries + 2,000 runs 60 s. Peak RSS growth of each command
  under 4 MiB (they stream). A `files migrate` killed mid-copy leaves at most one Axentra object without a mapping,
  which `files verify` reports as an orphan (`"ok": false`) and `files cleanup` deletes.
- Axentra adds about 10 MiB to a process after start (its import and services) and a transient start peak (about
  +60 MiB RSS high-water mark while the services start); nothing grows under load.

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

## Legacy paths and their removal preconditions (M12)

M12 is the contract step of the migration: an old implementation goes only when (1) the new one is fully tested,
(2) existing data has been migrated and checked, (3) the fallback period has passed, (4) no supported installation or
integration depends on it, (5) a valid backup and restore path exists and (6) the required security review has
passed. As of 2026-11-02 no switch is active in any production environment, no fallback period has started and the
independent security review is still running, so conditions 2, 3 and 6 hold for no path. What M12 could remove now
was dead or superseded code with no data or rollback dependency: Axentra starting on a pool of its own (now refused by
`AxentraServices.start`, and with it the duplicate `pool="axentra"` metric series), `AxentraServices.secret_box`
(superseded by `keyring_secret_box`), `AxentraLink.backfill_tenants` (superseded by `backfill-identity`, which also
copies names and suspensions) and `AxentraLink.reverse` (never called).

Common precondition for every row below, called **production evidence** here: the switch has been on in production
for at least two releases and at least 30 days without a rollback; the independent security review of that switch
has passed; and a restore drill of the pre-switch backup on a production copy has passed.

| Path (files, symbols) | Switch, default | Why it stays | Removal precondition |
|---|---|---|---|
| `identity_store.IdentityStore` reads of sessions, memberships, roles and tenants; tables `nodestro_memberships`, `nodestro_login_states` | `NODESTRO_IDENTITY_BACKEND`, default `nodestro` (this path) | Default path and the rollback target of `axentra` mode; `mirror` and `axentra` write both sides so that a rollback is a restart | Production evidence for `axentra` mode; `axentra reconcile-identity` prints `"ok": true` with no difference at the end of the window; the decision that `nodestro` and `mirror` mode are no longer supported (they are also the only modes of a server without the package). Then a migration drops the two tables and `AxentraIdentityStore` stops writing them |
| Tables `nodestro_tenants`, `nodestro_users`, `nodestro_sessions`, `nodestro_bootstrap_tokens`, `nodestro_axentra_ids` | none | Not duplicates: they hold Nodestro's ids, tenant status, the profile of `/api/auth/me`, the active tenant of a session, the setup links (G-5) and the id mapping, in every mode | Not planned. Only the columns that duplicate Axentra (session expiry and revocation, user identity) may go with the row above |
| Nodestro's OIDC client: `nodestro_api.identity.OidcClient`, `NodestroLoginFlow`, PKCE helpers, `ALGORITHMS`, `secure_url` | `NODESTRO_IDENTITY_BACKEND` `nodestro`/`mirror` (default) | The login of every server without Axentra identity, including a server without the package. `safe_return_path` and the routes are used in every mode and stay | Same as the first row: `axentra` mode the only supported identity mode |
| Identity mirror writes and `backfill-identity` / `reconcile-identity` (`axentra_identity`) | `mirror`/`axentra` | The mirror is application code in one transaction, not database triggers; backfill and reconciliation are the evidence for condition 2 | Removed together with the first row; database mirror triggers were never built, so there is nothing else to remove |
| Shadow modes (plan 4.6 `shadow`, `primary`, `only`) | – | Never implemented: `mirror` and `axentra` replace them | Nothing to remove |
| `server_settings.SecretBox` v1 (`nodestro:enc:v1:`), the v1 branch of `KeyringSecretBox`, `NODESTRO_SECRET_KEY` | `NODESTRO_SECRETS_FORMAT`, default `v1` | Default format and the only one of a server without Axentra; the desktop's MCP token vault (`mcp.vault`, `NODESTRO_VAULT_KEY`) uses the same class, so the class itself stays | For the server's values: the decision that new servers write `axs1` (RT-010); on every installation `secrets reencrypt --dry-run` reports `pending: 0` and `failed: []` and `nodestro_secrets_stored{format="v1"}` is 0; zero `nodestro_integration_operations_total{operation="secret_decrypt",detail="v1"}` on every API process for 30 days (workers have no metrics listener, hence also the stored count); review passed. Then `KeyringSecretBox(legacy=None)` retires v1 (already tested: such values read as `secrets.unreadable`, never deleted) and `NODESTRO_SECRET_KEY` becomes optional |
| Volume blobs of input files (`input_files._Blobs` on the data volume) | `NODESTRO_FILES_BACKEND`, default `volume` | Default path, the fallback read for files from before the switch and the rollback target (`files export-volume`); the desktop and Local Docker always use it | Per server: `files migrate` and `files verify` with `"ok": true`, then `files remove-source` after `NODESTRO_FILES_SOURCE_RETENTION_DAYS` (that mechanism exists). The code stays for the desktop |
| Audit and usage mirrors (`AxentraEvents`, `axentra reconcile`) | `NODESTRO_AXENTRA_AUDIT`, `_USAGE`, `_RUN_USAGE`, default off | Not a legacy path: Nodestro's records stay the source of truth (decided, see [Usage](#usage-end-state-and-run-usage)); the mirror is the integration | Not planned |
| Nodestro's migration runner (`storage_postgres.PostgresDatabase.migrate`) | none | Not a legacy path (M9 decided, see [Migrations](#migrations-m9-decided-nodestro-keeps-its-runner)) | Not planned |
| `NODESTRO_AXENTRA_ALLOW_SUPERUSER` | default `0` | Not made obsolete by the runtime role: it is the explicit development opt-in that keeps the role check strict by default; a single-role database (the owner serves) cannot start Axentra without it, and the env-based tests of nodestro-api run that way | Every development setup and test harness uses the owner/runtime roles (for example `NODESTRO_TEST_RUNTIME_ROLE=1` as the default) |
| `ndk_` API keys (`nodestro_api.serving`, `api_keys`) | – | The server serves no published APIs (M6); the desktop and Local Docker do | Not on the desktop. On a server: when publication comes, `axk_` keys plus the legacy verifier (see [API keys](#api-keys-m6-decision)), and the legacy verifier only after zero `ndk_` logins for an announced period |

The contract step itself is tracked as RT-036 in `docs/TODO.md`.
