"""MUP-ONA A9 RLS prototype (T-RLS-P1). Collected only when ``A9_RLS_ADMIN`` names a superuser connection to a TEST
server; otherwise the directory is left out entirely (no skip, so the CI skip lists stay unchanged).

    A9_RLS_ADMIN="host=127.0.0.1 port=55522 user=postgres password=... dbname=postgres" \\
        A9_RLS_DUMP=FX-PG-0v.sql.gz pytest tests/rls_prototype
"""

from __future__ import annotations

from collections.abc import Iterator
import os
from pathlib import Path
import sys

import pytest

if not os.environ.get("A9_RLS_ADMIN"):
    collect_ignore_glob = ["*.py"]
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

    import rls_support

    @pytest.fixture(scope="session")
    def prototype() -> Iterator[dict]:
        """The prototype database with the fixture restored; what the runtime role saw before the policies; the
        row-level security state of every table before and after the policies."""
        import psycopg

        setup = rls_support.prepare(os.environ.get("A9_RLS_DUMP", "FX-PG-0v.sql.gz"))
        before_state = rls_support.rls_state(setup.admin)
        before_tenants = {}
        with psycopg.connect(setup.runtime, autocommit=True) as runtime:
            for table in rls_support.POLICY_TABLES:
                rows = runtime.execute(f"SELECT DISTINCT tenant_id FROM {table} ORDER BY 1").fetchall()
                before_tenants[table] = [row[0] for row in rows]
        rls_support.apply_policies(setup)
        yield {
            "setup": setup,
            "before_tenants": before_tenants,
            "before_state": before_state,
            "after_state": rls_support.rls_state(setup.admin),
        }
