# Legacy PostgreSQL fixtures (MUP-ONA A0)

Tests on fresh databases do not show whether a new release still reads data an older release wrote. These dumps are
databases written by nodestro-runtime at `master` `802fa8f` (before the first MUP-ONA package changed any behavior), so
later packages can test upgrade, rollback and schema checks against real legacy data (plan section 5.2, scenarios
U1-U8). They are synthetic: no real tenants, texts or secrets.

| File | Server | Content |
|---|---|---|
| `FX-PG-0.sql.gz` | `postgres:17@sha256:748a18a54d3842d43bc156f0840d78029b8d349e0feca98c12b9c4ce3404a6b3` (PostgreSQL 17.11) | without pgvector |
| `FX-PG-0v.sql.gz` | `pgvector/pgvector:pg17@sha256:7d0a41464797aeb9862dfdc0bbf1526b82cfd324cdd8036df19e3545f4a9ccb8` (PostgreSQL 17.11, pgvector 0.8.7) | the same data; migration 0010 installed the `vector` extension |

Each dump has a `.sha256` file (`sha256sum -c FX-PG-0.sql.gz.sha256`).

## Content

Both dumps hold the same data, written by `make_legacy_pg.py`:

- migrations 0001-0010 applied by the runtime's migration runner, with their recorded checksums
  (`tests/fixtures/migration_checksums.json` lists the same values);
- two tenants, `tenant-a` and `tenant-b`, in the knowledge namespace `nodestro` with the same files (file ids are
  content addresses, so the ids and chunk sources are the same in both tenants) and the same chunk texts, plus one
  marker file per tenant (`marker-a.txt`: "aardvark amethyst"; `marker-b.txt`: "zebra zircon");
- vectors in two spaces per tenant: `active` for the fake model `fx-embed-a` (8 dimensions, every chunk embedded) and
  `building` for `fx-embed-b` (6 dimensions, a reindex cancelled after its first batch of two chunks);
- one zero vector per tenant in the active space (chunk of `zero-vector.txt`); all other vectors are finite;
- memory space `fixture-notes` (namespace `nodestro:fixture-notes`) with the same two files in both tenants and one
  file named after the tenant.

Everything is written through the public runtime API (`KnowledgeBase`, `PostgresIndex.reindex`, `MemorySpaces`),
except the zero vector, which is set with one `UPDATE` (the embedding models never return one). The `building` space
comes from a real reindex that was cancelled, not from SQL.

## Regenerating

Only with a reason in the PR: later packages compare against these exact bytes. `pg_dump` runs inside the server's
container, so the client version matches the server.

```bash
docker run -d --name fx-pg17 -e POSTGRES_PASSWORD=fx -p 55441:5432 \
  postgres:17@sha256:748a18a54d3842d43bc156f0840d78029b8d349e0feca98c12b9c4ce3404a6b3
docker run -d --name fx-pgvec -e POSTGRES_PASSWORD=fx -p 55442:5432 \
  pgvector/pgvector:pg17@sha256:7d0a41464797aeb9862dfdc0bbf1526b82cfd324cdd8036df19e3545f4a9ccb8
python tests/fixtures/legacy/make_legacy_pg.py --container fx-pg17 \
  --dsn "host=127.0.0.1 port=55441 user=postgres password=fx dbname=postgres" --out tests/fixtures/legacy/FX-PG-0.sql.gz
python tests/fixtures/legacy/make_legacy_pg.py --container fx-pgvec \
  --dsn "host=127.0.0.1 port=55442 user=postgres password=fx dbname=postgres" --out tests/fixtures/legacy/FX-PG-0v.sql.gz
```

The dump is `pg_dump --format=plain --no-owner --no-privileges`, compressed with `gzip -n -9` (no file name or time
stamp in the header). The SQL itself differs on every run (creation times, and the random key of pg_dump's
`\restrict` line), so a regenerated dump always gets a new checksum.

## Restoring

`tests/test_legacy_fixtures.py` (T-FX-1) restores a dump with psycopg alone (no `psql`): statements as they are, `COPY`
blocks through `cursor.copy`, psql meta-commands (`\restrict`, `\unrestrict`) left out. By hand:

```bash
createdb legacy && gunzip -c tests/fixtures/legacy/FX-PG-0.sql.gz | psql -v ON_ERROR_STOP=1 -d legacy
```

FX-PG-0v needs a server with pgvector; on a server without it the T-FX-1 case for FX-PG-0v is skipped (listed in
`tests/ci/skipped-*.txt`).
