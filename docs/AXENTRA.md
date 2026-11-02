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
| `NODESTRO_IDENTITY_BACKEND` | `nodestro` | `mirror`: Nodestro answers, every identity write also goes to Axentra in the same transaction; `axentra`: Axentra answers, both are written. Needs `on`; see [Identity](#identity-and-tenancy-m5) |

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
(`axentra.data.Database.on_pool`, see [One connection pool](#one-connection-pool-m10)).

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
  CHANGELOG. **The runtime pin must include that fix before any mirror runs under a runtime role.**

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

Instead, `tests/test_tenant_filters.py` fails when a PostgreSQL statement on a tenant-data table (`nodestro_documents`,
`_files`, `_chunks`, `_memory_files`, `_run_events`) does not mention `tenant_id`; the only exception is the run-event
housekeeping (by age, every tenant). The runtime role cannot alter the tables either. Revisit when Nodestro's store
runs inside per-request transactions anyway.

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
- **What is not mirrored.** Usage of workflow runs stays in the stored runs (the usage dashboard sums both). Mirroring
  it would need a per-run idempotency key and a decision on billing; until then Axentra's meters cover only usage
  outside runs. No limit is enforced from Axentra (`enforce=False`).

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

## What stays Nodestro's

- **Jobs.** Nodestro's job queue keeps its guarantees: one active job per `(tenant, kind, ref)`, the run fence
  (compare-and-set), interruption on lease loss, never a blind retry of model calls or external writes. Axentra's
  jobs are at-least-once with a reaper that re-queues, so they are not a drop-in replacement.
- **Workflow, run, knowledge and memory data**, tool approvals, MCP, execution permissions and the Studio UI.

## Rollback

Every switch can be turned off again without data changes: `NODESTRO_AXENTRA_AUDIT=0`, `..._USAGE=0`,
`NODESTRO_IDENTITY_BACKEND=mirror` or `nodestro` (see above), `NODESTRO_AXENTRA=off`. `NODESTRO_SECRETS_FORMAT=axs1` can go back to `v1` only while the Axentra keyring is still
configured (the `axs1` values written meanwhile need it to be read); keep `NODESTRO_AXENTRA=on` until they are moved
back, or restore the pre-switch backup. A full rollback restores the backup taken by `migrate`.
