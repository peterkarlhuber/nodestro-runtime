"""Audit round 2 (ops, OPS2-01): the backup that ``migrate --backup-dir`` takes before a schema migration runs
``pg_dump`` on a separate connection while the migration transaction is open. For a database written by a release
before migration checksums (schema table without the ``checksum`` column), the migration transaction has already run
``ALTER TABLE nodestro_schema_migrations ADD COLUMN checksum`` (ACCESS EXCLUSIVE) when it calls ``before_migration``,
so the dump's ``LOCK TABLE ... IN ACCESS SHARE MODE`` waits until the idle-in-transaction timeout ends the migration.

This test plays ``pg_dump``'s part: another connection takes ACCESS SHARE locks on the schema table inside
``before_migration``. Failed on master (lock timeout -> storage.backup_failed); fixed by adding the column
after the backup."""

from __future__ import annotations

from collections.abc import Iterator

import postgres_support
import pytest

psycopg = pytest.importorskip("psycopg")

from nodestro_runtime.storage_postgres import (  # noqa: E402
    POSTGRES_SCHEMA_VERSION,
    PostgresDatabase,
    postgres_migrations,
)


@pytest.fixture
def schema() -> Iterator[str]:
    postgres_support.require_postgres()
    name = postgres_support.new_schema()
    yield name
    postgres_support.drop_schema(name)


@pytest.mark.parametrize("old_table", [True, False], ids=["table-without-checksum-column", "null-checksums"])
def test_a_backup_on_another_connection_can_read_the_schema_table(schema: str, old_table: bool) -> None:
    steps = postgres_migrations()
    database = PostgresDatabase(postgres_support.DSN, schema=schema)
    try:
        database.migrate(migrations={v: s for v, s in steps.items() if v <= 4})  # the v0.2.0-preview.1 schema
        # As the owner: with NODESTRO_TEST_RUNTIME_ROLE=1 ``database`` connects as the restricted runtime role, which
        # may neither alter nor write the schema table (audit round 3, RT3-06).
        change = (
            "ALTER TABLE nodestro_schema_migrations DROP COLUMN checksum"
            if old_table
            else "UPDATE nodestro_schema_migrations SET checksum = NULL"
        )
        with psycopg.connect(postgres_support.DSN, autocommit=True) as owner:
            owner.execute(f'SET search_path TO "{schema}"')
            owner.execute(change)
        seen: list[tuple[int, int]] = []

        def dump_like_pg_dump(current: int, target: int) -> None:
            seen.append((current, target))
            with psycopg.connect(postgres_support.DSN, autocommit=True) as other:
                other.execute("SET lock_timeout = '3s'")
                with other.transaction():
                    other.execute(f'SET LOCAL search_path TO "{schema}"')
                    other.execute("LOCK TABLE nodestro_schema_migrations IN ACCESS SHARE MODE")

        assert database.migrate(before_migration=dump_like_pg_dump) == 4
        assert seen == [(4, POSTGRES_SCHEMA_VERSION)]
        # After the backup the table got its column and every applied migration its checksum.
        assert database._has_checksums() and database.schema_version == POSTGRES_SCHEMA_VERSION
        assert database._query("SELECT count(*) FROM nodestro_schema_migrations WHERE checksum IS NULL") == [(0,)]
    finally:
        database.close()
