# Contributing to nodestro-runtime

## Changes to the tenant SQL check or the migrations

A pull request that changes `src/nodestro_runtime/tenant_sql_check.py`, `src/nodestro_runtime/tenant_sql_allowlist.py`
or a file in `src/nodestro_runtime/migrations/postgres/` is ready to merge only when:

- a nodestro-api pull request with a temporary integration pin on this pull request's runtime commit shows a green
  `tenant-sql` job (the API scanned with this checker and these migrations), and both pull requests link each other;
- the job `tenant-sql-api` of this repository is green (an early warning; it does not replace the API job);
- a new migration file is added to `tests/fixtures/migration_checksums.json`; a released migration is never edited.

Background: `docs/AXENTRA.md`, "Row-level security on Nodestro's tables" (MUP-ONA A11, rule 3).

## Skipped tests

Every CI pytest step compares its skipped tests with `tests/ci/skipped-<job>.txt` (`scripts/check_skips.py`). A test
that starts or stops being skipped fails the job; update the file with the reason and say why in the pull request.
