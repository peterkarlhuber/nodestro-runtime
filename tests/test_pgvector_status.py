"""pgvector installed after migration 0010: what the runtime reports and what an operator runs (RT-048).

``test_states_on_a_real_server`` needs a PostgreSQL server that *has* pgvector, as a superuser connection string in
``NODESTRO_TEST_PGVECTOR`` (e.g. the ``pgvector/pgvector:pg17`` image); it creates and drops a database of its own,
because an extension belongs to a whole database. ``test_a_server_without_pgvector`` runs on
``NODESTRO_TEST_POSTGRES`` when that server has no pgvector (CI's ``postgres:17``). The stand-in cases check the
wording of every state without a server.

TABRION MIG-NOD-7: the catalog read and the state come from ``tabrion.postgres.extension_status``. The output must not
change: every case compares the whole answer with ``tests/fixtures/pgvector_status_before.json``, which the code before
the switch wrote (``tests/fixtures/make_pgvector_status.py``): the stand-in cases for every state (mode A/B wording),
the real servers for "unavailable" (mode A, pg-a) and "active", "installable", "other_schema" (mode B, pg-b).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import json
import os
from pathlib import Path
from typing import Any
import uuid

from postgres_support import drop_schema, new_schema, require_postgres
import pytest

from nodestro_runtime import storage_postgres
from nodestro_runtime.storage_postgres import PostgresDatabase, pgvector_status

PGVECTOR_DSN = os.environ.get("NODESTRO_TEST_PGVECTOR", "")
#: The output of the code before TABRION MIG-NOD-7, per case (``tests/fixtures/make_pgvector_status.py``).
BEFORE: dict[str, dict[str, Any]] = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "pgvector_status_before.json").read_text(encoding="utf-8")
)
#: The placeholders of the baseline: the pgvector version and the schema of the database.
VERSION, SCHEMA = "<version>", "nodestro"


class _Answers:
    """A stand-in database whose ``_read`` hands no connection to the reader (no PostgreSQL)."""

    def _read(self, read: Callable[[Any], Any]) -> Any:
        return read(None)


def _status(state: str, schema: str | None, installed: str | None, where: str | None, available: str | None) -> Any:
    from tabrion.postgres import ExtensionStatus

    return ExtensionStatus(
        name="vector",
        state=state,  # type: ignore[arg-type]
        installed_version=installed,
        available_version=available,
        extension_schema=where,
        schema=schema,
        command="(Tabrion's own command is not shown)",
    )


#: What ``extension_status`` answers in each case of the baseline.
STATUSES: dict[str, Any] = {
    "active": _status("active", SCHEMA, VERSION, SCHEMA, VERSION),
    "other_schema": _status("other_schema", SCHEMA, VERSION, "elsewhere", VERSION),
    "installable": _status("installable", SCHEMA, None, None, VERSION),
    "installable_quoted_schema": _status("installable", 'odd"name', None, None, VERSION),
    "installable_no_current_schema": _status("installable", None, None, None, VERSION),
    "unavailable": _status("unavailable", SCHEMA, None, None, None),
}


def _normalized(found: dict[str, Any], *, version: str | None, schema: str | None) -> dict[str, Any]:
    """``found`` with the server's pgvector version and the test's schema replaced by the baseline's placeholders."""

    def plain(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        if version:
            value = value.replace(version, VERSION)
        if schema and schema != SCHEMA:
            value = value.replace(schema, SCHEMA)
        return value

    return {key: plain(value) for key, value in found.items()}


def test_the_baseline_covers_every_state() -> None:
    assert set(BEFORE) == set(STATUSES)
    assert {case["state"] for case in BEFORE.values()} == {"active", "other_schema", "installable", "unavailable"}


@pytest.mark.parametrize("case", sorted(STATUSES))
def test_the_output_is_the_one_before_tabrion(case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []

    def answer(connection: Any, name: str, *, probe_type: str | None = None) -> Any:
        calls.append((connection, name, probe_type))
        return STATUSES[case]

    monkeypatch.setattr(storage_postgres, "extension_status", answer)
    assert pgvector_status(_Answers()) == BEFORE[case]  # type: ignore[arg-type]
    assert calls == [(None, "vector", "vector")]


@pytest.mark.parametrize("case", sorted(STATUSES))
def test_every_state_names_what_to_do(case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(storage_postgres, "extension_status", lambda *_args, **_kwargs: STATUSES[case])
    found = pgvector_status(_Answers())  # type: ignore[arg-type]
    state, command = found["state"], found["command"]
    assert state == STATUSES[case].state
    assert (command is None) == (state in ("active", "unavailable"))
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
    assert _normalized(found, version=None, schema=plain.schema) == BEFORE["unavailable"]  # mode A


def test_pgvector_status_reads_inside_a_transaction(plain: PostgresDatabase) -> None:
    """Inside a transaction the status is read on its connection (a savepoint guards it) and leaves it usable."""
    with plain.transaction():
        found = pgvector_status(plain)
        assert plain._query("SELECT 1") == [(1,)]
    assert pgvector_status(plain) == found


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
        found = pgvector_status(database)
        assert found["state"] == "active"
        version = found["version"]
        assert version and _normalized(found, version=version, schema=None) == BEFORE["active"]  # mode B
        # pgvector installed after migration 0010: the database has it only as an available extension.
        _run(dsn, "DROP EXTENSION vector")
        found = pgvector_status(database)
        assert found["state"] == "installable" and found["available_version"]
        assert found["command"] == 'CREATE EXTENSION vector SCHEMA "nodestro";'
        assert _normalized(found, version=version, schema=None) == BEFORE["installable"]
        # Installed, but in a schema the server's search_path (only NODESTRO_DATABASE_SCHEMA) does not see.
        _run(dsn, "CREATE EXTENSION vector SCHEMA elsewhere")
        found = pgvector_status(database)
        assert found["state"] == "other_schema" and found["extension_schema"] == "elsewhere"
        assert found["version"] == found["available_version"]
        assert _normalized(found, version=version, schema=None) == BEFORE["other_schema"]
        _run(dsn, found["command"])  # what the operator runs
        found = pgvector_status(database)
        assert found["state"] == "active" and found["command"] is None and found["extension_schema"] == "nodestro"
        rows = database._query("SELECT '[1,2,3]'::vector <=> '[1,2,3]'::vector")
        assert rows[0][0] == 0
    finally:
        database.close()
