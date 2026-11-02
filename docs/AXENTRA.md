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

Every switch can be turned off again without data changes: `NODESTRO_AXENTRA_AUDIT=0`, `..._USAGE=0`,
`NODESTRO_IDENTITY_BACKEND=mirror` or `nodestro` (see above), `NODESTRO_AXENTRA=off`. `NODESTRO_SECRETS_FORMAT=axs1` can go back to `v1` only while the Axentra keyring is still
configured (the `axs1` values written meanwhile need it to be read); keep `NODESTRO_AXENTRA=on` until they are moved
back, or restore the pre-switch backup. A full rollback restores the backup taken by `migrate`.
