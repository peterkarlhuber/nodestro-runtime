# Source of the golden rollback snapshots

The snapshots in this directory are the "before" picture of the server-profile steps of the Tabrion adoption
(TABRION QA-13; used by MIG-NOD-3, -4, -5 and -7). They were written from the old code, before any of these steps:

- commit: `463e0d99ca66cfdf61f9581e271fc5e0361e0970` (master, "Merge pull request #144"), Axentra pin `9c4666f`
- tool: `python tests/rollback_snapshots.py --dsn <superuser connection string> write`
- `catalog.json` was rewritten from the same commit and pin with the review fix round's tool (per-table Axentra
  entries, policies and functions of the whole schema, ACL entries, memberships; `qa-13-25-review.md` M-1, M-6, L-7);
  the other four files were byte-identical in that run
- server: PostgreSQL 16; CI compares them on PostgreSQL 17, with and without pgvector, as the superuser and with
  `NODESTRO_TEST_RUNTIME_ROLE=1` (`tests/test_rollback_snapshots.py`)

| File | What it holds | Evidence for |
|---|---|---|
| `catalog.json` | Row-level security and runtime role privileges per table after `grant_runtime_role`, Nodestro's and (by name, with counts) Axentra's; every policy and function (owner, `SECURITY DEFINER`, `proconfig`, runtime `EXECUTE`); every ACL entry of the schema's objects (`relacl`, `attacl`, `proacl`, `nspacl`: grantor, grantee incl. `PUBLIC`, privilege, grant option); default, schema and database privileges (incl. `TEMPORARY`), the run roles' database ACL entries and memberships | MIG-NOD-5 |
| `roles.json` | Role decision table, eight variants (`inspect_role`, `grant_runtime_role`, `role_info`) | MIG-NOD-5 |
| `errors.json` | Code, parameters and message of the database error paths; `message_has_canary` (finding B-06-2) | MIG-NOD-3 |
| `settings.json` | `SHOW` extract per kind of connection (transaction, session lock, `DatabaseSource` with a tenant setting, the pool lent to Axentra, a pool without schema) | MIG-NOD-4 |
| `schema.json` | Schema dump of Nodestro's tables from the catalog (no extension objects) | every step: no schema change |

`errors.json` recorded the old messages as they were: three of them contained the canary value of the server's
`DETAIL` text (`message_has_canary: true`). MIG-NOD-3 closed B-06-2 and rewrote `errors.json` with the tool (only
those three messages changed: they now end with Tabrion's value-free text; the other four files were identical) and
the expectation in `tests/test_rollback_snapshots.py`. A revert of MIG-NOD-3 restores the old `errors.json` with it. A pull request that changes another snapshot on
purpose (for example a new migration) rewrites the file with the tool and says why. A rollback (revert pull request)
is verified by the same files: after the revert, `check` reports "snapshots identical" again.

MIG-NOD-7 (extension status) is **not** covered by these files: `schema.json` leaves extension objects out, and no
file records `pgvector_status`, whose answer depends on the server (pgvector mode A or B). MIG-NOD-7 writes its own
"before" output of `pgvector_status` in both CI modes (pg-a, pg-b) from the old code first (review L-6).
