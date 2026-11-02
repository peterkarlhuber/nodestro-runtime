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

## What stays Nodestro's

- **Jobs.** Nodestro's job queue keeps its guarantees: one active job per `(tenant, kind, ref)`, the run fence
  (compare-and-set), interruption on lease loss, never a blind retry of model calls or external writes. Axentra's
  jobs are at-least-once with a reaper that re-queues, so they are not a drop-in replacement.
- **Workflow, run, knowledge and memory data**, tool approvals, MCP, execution permissions and the Studio UI.

## Rollback

Every switch can be turned off again without data changes: `NODESTRO_AXENTRA_AUDIT=0`, `..._USAGE=0`,
`NODESTRO_AXENTRA=off`. `NODESTRO_SECRETS_FORMAT=axs1` can go back to `v1` only while the Axentra keyring is still
configured (the `axs1` values written meanwhile need it to be read); keep `NODESTRO_AXENTRA=on` until they are moved
back, or restore the pre-switch backup. A full rollback restores the backup taken by `migrate`.
