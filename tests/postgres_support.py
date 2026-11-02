"""PostgreSQL for the tests: ``NODESTRO_TEST_POSTGRES`` names a database (a libpq connection string).

Each test gets its own schema, dropped afterwards. Without the variable the PostgreSQL cases are skipped, unless
``NODESTRO_REQUIRE_POSTGRES=1`` (CI), which turns the missing database into a failure.

``NODESTRO_TEST_RUNTIME_ROLE=1`` runs every PostgreSQL case as a production server does: the role of
``NODESTRO_TEST_POSTGRES`` is the **owner** (it runs every migration and owns the tables), and every
:class:`~nodestro_runtime.storage_postgres.PostgresDatabase` the tests open on it connects as the restricted runtime
role ``nodestro_test_runtime`` instead (no superuser, no ``BYPASSRLS``, owns nothing, privileges from
:func:`~nodestro_runtime.storage_postgres.grant_runtime_role` only). So Nodestro's and Axentra's code paths are proven
to work without DDL rights and under Axentra's row-level security (see :func:`install_runtime_role_mode`).
"""

from __future__ import annotations

import os
import threading
from typing import Any
import uuid

import pytest

DSN = os.environ.get("NODESTRO_TEST_POSTGRES", "")
REQUIRED = os.environ.get("NODESTRO_REQUIRE_POSTGRES") == "1"
RUNTIME_ROLE_MODE = os.environ.get("NODESTRO_TEST_RUNTIME_ROLE") == "1"
RUNTIME_ROLE = "nodestro_test_runtime"
RUNTIME_PASSWORD = "nodestro-test-runtime-pw"


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


def runtime_dsn() -> str:
    """``DSN`` with the runtime role's credentials."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    parts = conninfo_to_dict(DSN)
    parts.update(user=RUNTIME_ROLE, password=RUNTIME_PASSWORD)
    return make_conninfo("", **parts)


def install_runtime_role_mode() -> None:
    """``NODESTRO_TEST_RUNTIME_ROLE=1``: databases opened on ``DSN`` connect as the runtime role; their ``migrate``
    runs as the owner (``DSN``) and then grants the runtime role its privileges, as ``nodestro-server migrate`` and
    ``db grant-runtime-role`` do on a server."""
    import psycopg
    from psycopg import sql

    from nodestro_runtime import storage_postgres

    with psycopg.connect(DSN, autocommit=True) as admin:
        if admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (RUNTIME_ROLE,)).fetchone() is None:
            admin.execute(
                sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD {}").format(
                    sql.Identifier(RUNTIME_ROLE), sql.Literal(RUNTIME_PASSWORD)
                )
            )
    cls = storage_postgres.PostgresDatabase
    original_init, original_migrate, original_close = cls.__init__, cls.migrate, cls.close
    runtime = runtime_dsn()
    granting = threading.Lock()  # concurrent GRANTs on one schema fail with "tuple concurrently updated"

    def init(self, conninfo, *args, **kwargs):  # type: ignore[no-untyped-def]
        if conninfo == DSN:
            self._test_owner_dsn = DSN
            conninfo = runtime
        original_init(self, conninfo, *args, **kwargs)

    def migrate(self, **kwargs):  # type: ignore[no-untyped-def]
        if getattr(self, "_test_owner_dsn", None) is None:
            return original_migrate(self, **kwargs)
        if self.in_transaction:
            raise RuntimeError("migrate() cannot run inside a transaction")
        version = original_migrate(owner_of(self), **kwargs)
        with granting:
            report = storage_postgres.grant_runtime_role(owner_of(self), RUNTIME_ROLE)
        assert report["restricted"], report
        return version

    def owner(self):  # type: ignore[no-untyped-def]
        if getattr(self, "_test_owner_dsn", None) is None:
            return self
        found = getattr(self, "_test_owner", None)
        if found is None:
            found = cls.__new__(cls)
            original_init(found, self._test_owner_dsn, schema=self.schema, pool_size=2)
            self._test_owner = found
        return found

    def close(self):  # type: ignore[no-untyped-def]
        found = getattr(self, "_test_owner", None)
        if found is not None:
            original_close(found)
        original_close(self)

    cls.__init__ = init  # type: ignore[method-assign]
    cls.migrate = migrate  # type: ignore[method-assign]
    cls.close = close  # type: ignore[method-assign]
    cls._test_owner_of = owner  # type: ignore[attr-defined]


def owner_of(database: Any) -> Any:
    """The owner role's view of ``database`` (same schema): for test checks and test setup that a runtime role may not
    do (DDL, reading Axentra's rows without a scope). Without ``NODESTRO_TEST_RUNTIME_ROLE`` it is ``database``."""
    found = getattr(database, "_test_owner_of", None)
    return database if found is None else found()
