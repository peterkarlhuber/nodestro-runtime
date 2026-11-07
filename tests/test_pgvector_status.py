"""pgvector installed after migration 0010: what the runtime reports and what an operator runs (RT-048).

``test_states_on_a_real_server`` needs a PostgreSQL server that *has* pgvector, as a superuser connection string in
``NODESTRO_TEST_PGVECTOR`` (e.g. the ``pgvector/pgvector:pg17`` image); it creates and drops a database of its own,
because an extension belongs to a whole database. ``test_a_server_without_pgvector`` runs on
``NODESTRO_TEST_POSTGRES`` when that server has no pgvector (CI's ``postgres:17``). The stand-in cases check the
wording of every state without a server.
"""

from __future__ import annotations

from collections.abc import Iterator
import os
from typing import Any
import uuid

from postgres_support import drop_schema, new_schema, require_postgres
import pytest

from nodestro_runtime.storage_postgres import PostgresDatabase, pgvector_status

PGVECTOR_DSN = os.environ.get("NODESTRO_TEST_PGVECTOR", "")


class _Answers:
    """A stand-in database that answers the status query with fixed values (no PostgreSQL)."""

    def __init__(self, *row: Any) -> None:
        self.row = row

    def _query(self, statement: str, params: Any = None) -> list[tuple[Any, ...]]:
        assert statement.startswith("SELECT to_regtype('vector')")
        return [self.row]


@pytest.mark.parametrize(
    ("row", "state", "command"),
    [
        ((True, "nodestro", "0.8.1", "nodestro", "0.8.1"), "active", None),
        (
            (False, "nodestro", "0.8.1", "public", "0.8.1"),
            "other_schema",
            'ALTER EXTENSION vector SET SCHEMA "nodestro";',
        ),
        ((False, 'odd"name', None, None, "0.8.1"), "installable", 'CREATE EXTENSION vector SCHEMA "odd""name";'),
        ((False, "public", None, None, None), "unavailable", None),
    ],
)
def test_every_state_names_what_to_do(row: tuple[Any, ...], state: str, command: str | None) -> None:
    found = pgvector_status(_Answers(*row))  # type: ignore[arg-type]
    assert found["state"] == state and found["command"] == command
    if command is not None:
        assert command in found["message"] and "restart" in found["message"]
    if state == "unavailable":
        assert "Install the pgvector package" in found["message"]


@pytest.fixture
def plain() -> Iterator[PostgresDatabase]:
    require_postgres()
    schema = new_schema()
    database = PostgresDatabase(os.environ["NODESTRO_TEST_POSTGRES"], schema=schema)
    try:
        database.migrate()
        yield database
    finally:
        database.close()
        drop_schema(schema)


def test_a_server_without_pgvector(plain: PostgresDatabase) -> None:
    found = pgvector_status(plain)
    if found["state"] != "unavailable":
        pytest.skip(f"this PostgreSQL server has pgvector ({found['state']})")
    assert found["command"] is None and found["version"] is None and found["available_version"] is None
    assert found["schema"] == plain.schema


@pytest.fixture
def server_with_pgvector() -> Iterator[str]:
    """A fresh database on a server that has pgvector, dropped afterwards."""
    if not PGVECTOR_DSN:
        # MUP-ONA T-PG-2: the pg-b job sets it and NODESTRO_REQUIRE_PGVECTOR=1, which turns this skip into a failure
        if os.environ.get("NODESTRO_REQUIRE_PGVECTOR") == "1":
            pytest.fail("NODESTRO_REQUIRE_PGVECTOR=1 but NODESTRO_TEST_PGVECTOR names no server")
        pytest.skip("NODESTRO_TEST_PGVECTOR is not set (a PostgreSQL server with pgvector)")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    name = f"nodestro_pgvector_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(PGVECTOR_DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield make_conninfo("", **{**conninfo_to_dict(PGVECTOR_DSN), "dbname": name})
    finally:
        with psycopg.connect(PGVECTOR_DSN, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def _run(dsn: str, statement: str) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(statement)  # type: ignore[arg-type]


def test_states_on_a_real_server(server_with_pgvector: str) -> None:
    dsn = server_with_pgvector
    _run(dsn, "CREATE SCHEMA elsewhere")
    database = PostgresDatabase(dsn, schema="nodestro")
    try:
        database.migrate()  # 0010 installs pgvector into the schema, as on a fresh server
        assert pgvector_status(database)["state"] == "active"
        # pgvector installed after migration 0010: the database has it only as an available extension.
        _run(dsn, "DROP EXTENSION vector")
        found = pgvector_status(database)
        assert found["state"] == "installable" and found["available_version"]
        assert found["command"] == 'CREATE EXTENSION vector SCHEMA "nodestro";'
        # Installed, but in a schema the server's search_path (only NODESTRO_DATABASE_SCHEMA) does not see.
        _run(dsn, "CREATE EXTENSION vector SCHEMA elsewhere")
        found = pgvector_status(database)
        assert found["state"] == "other_schema" and found["extension_schema"] == "elsewhere"
        assert found["version"] == found["available_version"]
        _run(dsn, found["command"])  # what the operator runs
        found = pgvector_status(database)
        assert found["state"] == "active" and found["command"] is None and found["extension_schema"] == "nodestro"
        rows = database._query("SELECT '[1,2,3]'::vector <=> '[1,2,3]'::vector")
        assert rows[0][0] == 0
    finally:
        database.close()
