"""PostgreSQL for the tests: ``NODESTRO_TEST_POSTGRES`` names a database (a libpq connection string).

Each test gets its own schema, dropped afterwards. Without the variable the PostgreSQL cases are skipped, unless
``NODESTRO_REQUIRE_POSTGRES=1`` (CI), which turns the missing database into a failure.
"""

from __future__ import annotations

import os
import uuid

import pytest

DSN = os.environ.get("NODESTRO_TEST_POSTGRES", "")
REQUIRED = os.environ.get("NODESTRO_REQUIRE_POSTGRES") == "1"


def require_postgres() -> str:
    """The test database, or skip (fail when PostgreSQL is required)."""
    if not DSN:
        if REQUIRED:
            pytest.fail("NODESTRO_REQUIRE_POSTGRES=1 but NODESTRO_TEST_POSTGRES names no database")
        pytest.skip("NODESTRO_TEST_POSTGRES is not set")
    return DSN


def new_schema() -> str:
    return f"nodestro_test_{uuid.uuid4().hex[:16]}"


def drop_schema(schema: str) -> None:
    import psycopg
    from psycopg import sql

    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
