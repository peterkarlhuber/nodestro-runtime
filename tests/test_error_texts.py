"""TABRION MIG-NOD-3 (finding B-06-2): the backup and migration messages hold no data values.

A failed backup (``storage.backup_failed``) or migration step (``storage.migration_failed``) used to end with
``str(exc)``: for PostgreSQL that is the server's text with ``DETAIL`` (``Key (email)=(...) already exists``), and for
any other error whatever its text quoted. :func:`nodestro_runtime.error_texts.failure_text` replaces it on both
stores. A canary value is put into every kind of failure and must not reach the message; the codes and parameters
stay.
"""

from __future__ import annotations

from collections.abc import Iterator
import errno
import os
from pathlib import Path
import sqlite3

import postgres_support
import pytest

from nodestro_runtime.error_texts import failure_text
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.storage import SQLiteStore

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
    """Value-free operator guidance of a hook (nodestro-api's backup) stays visible."""
    error = NodestroError("storage.backup_required", "take a backup first (--backup-dir DIR runs pg_dump)")
    assert failure_text(error) == "take a backup first (--backup-dir DIR runs pg_dump)"


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
        f"pg_dump: error: query failed: ERROR:  duplicate key\npg_dump: detail: Key (email)=({CANARY}) exists",
        # A primary message that quotes the value, without any DETAIL line (review condition b).
        f'pg_dump failed: pg_dump: error: query failed: ERROR:  invalid input syntax for type integer: "{CANARY}"',
        f'restore failed: ERROR:  invalid input syntax for type integer: "{CANARY}"',
    ],
)
def test_server_text_in_other_forms_also_gives_only_the_code(message: str) -> None:
    assert failure_text(NodestroError("storage.backup_failed", message)) == "storage.backup_failed"


def test_an_error_raised_while_handling_a_database_error_names_itself() -> None:
    """``__context__`` is only what was being handled, not the cause: the error names its own class."""
    try:
        try:
            raise _sqlite_unique_error()
        except sqlite3.Error:
            raise ValueError(f"cannot convert {CANARY}")  # noqa: B904 - no explicit cause on purpose
    except ValueError as exc:
        assert failure_text(exc) == "ValueError"
    try:
        try:
            raise _sqlite_unique_error()
        except sqlite3.Error:
            raise NodestroError("storage.backup_failed", "backup tool missing")  # noqa: B904
    except NodestroError as exc:
        assert failure_text(exc) == "backup tool missing"


def test_a_nodestro_error_caused_by_a_database_error_gives_the_safe_message() -> None:
    try:
        try:
            raise _sqlite_unique_error()
        except sqlite3.Error as inner:
            raise NodestroError("storage.backup_failed", f"copy failed: {inner}") from inner
    except NodestroError as exc:
        assert failure_text(exc).startswith("unique violation (SQLite SQLITE_CONSTRAINT_UNIQUE")


def test_a_database_error_behind_a_non_database_orig_is_found() -> None:
    class Adapter(Exception):
        orig = ValueError(CANARY)

    try:
        raise Adapter(CANARY) from _sqlite_unique_error()
    except Adapter as exc:
        assert failure_text(exc).startswith("unique violation (SQLite")


def test_a_pgcode_or_a_tabrion_error_in_the_chain_counts() -> None:
    from tabrion.errors import StatementError, classify

    class Psycopg2Like(Exception):
        pgcode = "23505"

    try:
        raise RuntimeError(CANARY) from Psycopg2Like(f"DETAIL:  Key (email)=({CANARY})")
    except RuntimeError as exc:
        assert failure_text(exc) == "unique violation (SQLSTATE 23505)"
    tabrion_error = StatementError("sqlite.statement_failed", classification=classify(_sqlite_unique_error()))
    try:
        raise RuntimeError(CANARY) from tabrion_error
    except RuntimeError as exc:
        text = failure_text(exc)
    assert text == str(tabrion_error) and CANARY not in text


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


# ---------------------------------------------------------------------------------------------------------------------
# Run jobs (jobs.py): the error of a job whose run cannot be executed
# ---------------------------------------------------------------------------------------------------------------------


class _UniqueViolation(Exception):
    """Stands in for psycopg's error: a SQLSTATE, and the server's DETAIL in its text."""

    sqlstate = "23505"


class _Runs:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def execute_claimed(self, _run_id: str) -> None:
        raise self.error


class _Workspace:
    def __init__(self, error: Exception) -> None:
        self.runs = _Runs(error)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            _UniqueViolation(f"duplicate key value\nDETAIL:  Key (email)=({CANARY}) already exists."),
            "_UniqueViolation: unique violation (SQLSTATE 23505)",
        ),
        (ValueError(f"bad row {CANARY}"), "ValueError: ValueError"),
        (NodestroError("run.not_found", "The run is gone"), "run.not_found: The run is gone"),
    ],
)
def test_a_job_whose_run_cannot_be_executed_stores_no_value(
    postgres: object, error: Exception, expected: str, caplog: pytest.LogCaptureFixture
) -> None:
    import asyncio

    from nodestro_runtime.jobs import RUN_JOB, JobQueue, RunWorker

    queue = JobQueue(postgres)  # type: ignore[arg-type]
    postgres.migrate()  # type: ignore[attr-defined]
    job = queue.enqueue("alpha", RUN_JOB, "run-1")
    assert job is not None
    with caplog.at_level("WARNING", logger="nodestro_runtime.jobs"):
        asyncio.run(RunWorker(queue, lambda _tenant: _Workspace(error), name="w1").run_once())
    finished = queue.get(job.id)
    assert finished is not None and finished.status == "failed"
    assert finished.error == expected
    assert CANARY not in caplog.text
