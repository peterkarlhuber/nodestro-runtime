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

- **Jobs.** Nodestro's job queue keeps its guarantees: one active job per `(tenant, kind, ref)`, the run fence
  (compare-and-set), interruption on lease loss, never a blind retry of model calls or external writes. Axentra's
  jobs are at-least-once with a reaper that re-queues, so they are not a drop-in replacement.
- **Workflow, run, knowledge and memory data**, tool approvals, MCP, execution permissions and the Studio UI.

## Rollback

Every switch can be turned off again without data changes: `NODESTRO_FILES_BACKEND=volume` (after
`files export-volume`, see [Files](#files-m8)), `NODESTRO_AXENTRA_AUDIT=0`, `..._USAGE=0`,
`NODESTRO_IDENTITY_BACKEND=mirror` or `nodestro` (see above), `NODESTRO_AXENTRA=off`. `NODESTRO_SECRETS_FORMAT=axs1` can go back to `v1` only while the Axentra keyring is still
configured (the `axs1` values written meanwhile need it to be read); keep `NODESTRO_AXENTRA=on` until they are moved
back, or restore the pre-switch backup. A full rollback restores the backup taken by `migrate`.
