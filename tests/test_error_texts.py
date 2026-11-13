"""TABRION MIG-NOD-3 (finding B-06-2): the backup and migration messages hold no data values.

A failed backup (``storage.backup_failed``) or migration step (``storage.migration_failed``) used to end with
``str(exc)``: for PostgreSQL that is the server's text with ``DETAIL`` (``Key (email)=(...) already exists``), and for
any other error whatever its text quoted. :func:`nodestro_runtime.storage.failure_text` replaces it on both stores. A
canary value is put into every kind of failure and must not reach the message; the codes and parameters stay.
"""

from __future__ import annotations

from collections.abc import Iterator
import errno
import os
from pathlib import Path
import sqlite3

import postgres_support
import pytest

from nodestro_runtime.problems import NodestroError
from nodestro_runtime.storage import SQLiteStore, failure_text

CANARY = "canary-nod3@example.com"


# ---------------------------------------------------------------------------------------------------------------------
# failure_text
# ---------------------------------------------------------------------------------------------------------------------


def _sqlite_unique_error() -> sqlite3.Error:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE t (email TEXT UNIQUE)")
        connection.execute("INSERT INTO t VALUES (?)", (CANARY,))
        connection.execute("INSERT INTO t VALUES (?)", (CANARY,))
    except sqlite3.Error as exc:
        return exc
    finally:
        connection.close()
    raise AssertionError("no error")


def test_a_sqlite_error_gives_tabrions_safe_message() -> None:
    assert failure_text(_sqlite_unique_error()) == (
        "unique violation (SQLite SQLITE_CONSTRAINT_UNIQUE, constraint t.email, table t, column email)"
    )


def test_a_database_error_wrapped_in_another_error_is_found() -> None:
    try:
        try:
            raise _sqlite_unique_error()
        except sqlite3.Error as inner:
            raise RuntimeError(f"step failed for {CANARY}") from inner
    except RuntimeError as exc:
        text = failure_text(exc)
    assert text.startswith("unique violation (SQLite") and CANARY not in text


def test_a_nodestro_error_keeps_its_own_message() -> None:
    assert (
        failure_text(NodestroError("storage.backup_failed", "pg_dump is not installed")) == "pg_dump is not installed"
    )


def test_a_nodestro_error_with_server_text_gives_only_its_code() -> None:
    error = NodestroError(
        "storage.backup_failed", f"pg_dump failed: duplicate key value\nDETAIL:  Key (email)=({CANARY}) already exists."
    )
    assert failure_text(error) == "storage.backup_failed"


@pytest.mark.parametrize(
    "message",
    [
        f"pg_dump failed: {'x' + chr(10) + 'DETAIL:  Key (email)=(' + CANARY + ')'!r}",  # repr form: \\nDETAIL:
        f"insert failed [SQL: INSERT INTO t VALUES (%(email)s)] [parameters: {{'email': '{CANARY}'}}]",
    ],
)
def test_server_text_in_other_forms_also_gives_only_the_code(message: str) -> None:
    assert failure_text(NodestroError("storage.backup_failed", message)) == "storage.backup_failed"


def test_an_error_raised_while_handling_a_database_error_names_that_error() -> None:
    try:
        try:
            raise _sqlite_unique_error()
        except sqlite3.Error:
            raise ValueError(f"cannot convert {CANARY}")  # noqa: B904 - no explicit cause on purpose
    except ValueError as exc:
        text = failure_text(exc)
    assert text.startswith("unique violation (SQLite") and CANARY not in text


def test_an_os_error_gives_its_system_error_name_without_the_path() -> None:
    text = failure_text(OSError(errno.ENOSPC, "No space left on device", f"/data/{CANARY}.db"))
    assert text == f"OSError (ENOSPC: {os.strerror(errno.ENOSPC)})"
    assert failure_text(FileNotFoundError(errno.ENOENT, "gone", CANARY)).startswith("FileNotFoundError (ENOENT: ")


@pytest.mark.parametrize(
    "error", [ValueError(CANARY), RuntimeError(f"bad row {CANARY}"), OSError(CANARY), KeyError(CANARY)]
)
def test_any_other_error_gives_only_its_class_name(error: Exception) -> None:
    assert failure_text(error) == type(error).__name__


# ---------------------------------------------------------------------------------------------------------------------
# SQLite store
# ---------------------------------------------------------------------------------------------------------------------


@pytest.fixture
def sqlite_store(tmp_path: Path) -> Iterator[SQLiteStore]:
    opened = SQLiteStore(tmp_path / "nodestro.db")
    with opened.maintenance() as connection:
        connection.execute("CREATE TABLE canary (email TEXT UNIQUE)")
        connection.execute("INSERT INTO canary VALUES (?)", (CANARY,))
    yield opened
    opened.close()


def _duplicate(connection: sqlite3.Connection) -> None:
    connection.execute("INSERT INTO canary VALUES (?)", (CANARY,))


def _raises_with_value(_connection: sqlite3.Connection) -> None:
    raise ValueError(f"cannot convert {CANARY}")


@pytest.mark.parametrize(
    ("step", "expected"),
    [
        (_duplicate, "unique violation (SQLite SQLITE_CONSTRAINT_UNIQUE, constraint canary.email"),
        (_raises_with_value, "ValueError"),
    ],
)
def test_a_failing_sqlite_step_names_no_value(sqlite_store: SQLiteStore, step: object, expected: str) -> None:
    with pytest.raises(NodestroError) as caught:
        sqlite_store.migrate(before_migration=lambda *_: None, migrations={1: step}, target=1)  # type: ignore[dict-item]
    assert caught.value.code == "storage.migration_failed"
    assert caught.value.params == {"found": 0, "supported": 1, "step": 1}
    assert expected in str(caught.value) and CANARY not in str(caught.value)
    assert CANARY not in repr(caught.value.problem.model_dump())


def test_a_failing_sqlite_backup_names_no_value(sqlite_store: SQLiteStore) -> None:
    def backup(_current: int, _target: int) -> None:
        raise ValueError(f"backup of {CANARY} failed")

    with pytest.raises(NodestroError) as caught:
        sqlite_store.migrate(before_migration=backup, migrations={1: _duplicate}, target=1)
    assert caught.value.code == "storage.backup_failed"
    assert str(caught.value).endswith("nothing was changed: ValueError")
    assert isinstance(caught.value.__cause__, ValueError)  # the original stays chained for the redacting log filter


# ---------------------------------------------------------------------------------------------------------------------
# PostgreSQL store
# ---------------------------------------------------------------------------------------------------------------------


@pytest.fixture
def postgres() -> Iterator[object]:
    pytest.importorskip("psycopg")
    postgres_support.require_postgres()
    from nodestro_runtime.storage_postgres import PostgresDatabase

    schema = postgres_support.new_schema()
    database = PostgresDatabase(postgres_support.DSN, schema=schema)
    try:
        yield database
    finally:
        database.close()
        postgres_support.drop_schema(schema)


def _first(sql: str) -> dict[int, tuple[str, str]]:
    return {1: ("0001_canary.sql", sql)}


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        # DETAIL: Key (email)=(<canary>) already exists.
        (
            f"CREATE TABLE canary (email text UNIQUE); INSERT INTO canary VALUES ('{CANARY}'), ('{CANARY}')",
            "unique violation (SQLSTATE 23505, constraint canary_email_key, table canary)",
        ),
        # The primary message itself quotes the value: invalid input syntax for type integer: "<canary>".
        (f"SELECT '{CANARY}'::integer", "invalid data (SQLSTATE 22P02)"),
        # DETAIL: Failing row contains (<canary>).
        (
            f"CREATE TABLE canary (email text CHECK (email = 'x')); INSERT INTO canary VALUES ('{CANARY}')",
            "check violation (SQLSTATE 23514, constraint canary_email_check, table canary)",
        ),
    ],
)
def test_a_failing_postgres_step_names_no_value(postgres: object, statement: str, expected: str) -> None:
    with pytest.raises(NodestroError) as caught:
        postgres.migrate(migrations=_first(statement))  # type: ignore[attr-defined]
    message = str(caught.value)
    assert caught.value.code == "storage.migration_failed"
    assert caught.value.params == {"found": 0, "supported": 1, "step": 1}
    assert message == f"The schema migration to version 1 failed; the database is unchanged: {expected}"
    assert CANARY not in repr(caught.value.problem.model_dump())


def test_a_failing_postgres_backup_names_no_value(postgres: object) -> None:
    import psycopg

    postgres.migrate(migrations=_first("CREATE TABLE canary (email text UNIQUE)"))  # type: ignore[attr-defined]

    def backup(_current: int, _target: int) -> None:
        with psycopg.connect(postgres_support.DSN, autocommit=True) as conn:
            conn.execute("CREATE TEMPORARY TABLE canary_backup (email text UNIQUE)")
            conn.execute("INSERT INTO canary_backup VALUES (%s), (%s)", (CANARY, CANARY))

    steps = {**_first("CREATE TABLE canary (email text UNIQUE)"), 2: ("0002_next.sql", "SELECT 1")}
    with pytest.raises(NodestroError) as caught:
        postgres.migrate(migrations=steps, before_migration=backup)  # type: ignore[attr-defined]
    assert caught.value.code == "storage.backup_failed"
    assert caught.value.params == {"found": 1, "supported": 2}
    assert str(caught.value).endswith(
        "nothing was changed: unique violation (SQLSTATE 23505, constraint canary_backup_email_key,"
        " table canary_backup)"
    )
    assert CANARY not in repr(caught.value.problem.model_dump())
