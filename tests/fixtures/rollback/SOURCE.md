# Source of the golden rollback snapshots

The snapshots in this directory are the "before" picture of the server-profile steps of the Tabrion adoption
(TABRION QA-13; used by MIG-NOD-3, -4, -5 and -7). They were written from the old code, before any of these steps:

- commit: `463e0d99ca66cfdf61f9581e271fc5e0361e0970` (master, "Merge pull request #144"), Axentra pin `9c4666f`
- tool: `python tests/rollback_snapshots.py --dsn <superuser connection string> write`
- server: PostgreSQL 16; CI compares them on PostgreSQL 17, with and without pgvector, as the superuser and with
  `NODESTRO_TEST_RUNTIME_ROLE=1` (`tests/test_rollback_snapshots.py`)

| File | What it holds | Evidence for |
|---|---|---|
| `catalog.json` | Row-level security and runtime role privileges per Nodestro table after `grant_runtime_role`; privilege sets on Axentra's tables; default, schema and database privileges | MIG-NOD-5 |
| `roles.json` | Role decision table, eight variants (`inspect_role`, `grant_runtime_role`, `role_info`) | MIG-NOD-5 |
| `errors.json` | Code, parameters and message of the database error paths; `message_has_canary` (finding B-06-2) | MIG-NOD-3 |
| `settings.json` | `SHOW` extract per kind of connection (transaction, session lock, `DatabaseSource` with a tenant setting, the pool lent to Axentra, a pool without schema) | MIG-NOD-4, MIG-NOD-7 |
| `schema.json` | Schema dump of Nodestro's tables from the catalog (no extension objects) | every step: no schema change |

`errors.json` records the old messages as they are: three of them contain the canary value of the server's `DETAIL`
text (`message_has_canary: true`). MIG-NOD-3 closes B-06-2, rewrites this file and the expectation in
`tests/test_rollback_snapshots.py`, so the change shows in the review. A pull request that changes another snapshot on
purpose (for example a new migration) rewrites the file with the tool and says why. A rollback (revert pull request)
is verified by the same files: after the revert, `check` reports "snapshots identical" again.
