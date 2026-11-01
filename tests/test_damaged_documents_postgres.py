"""PostgreSQL listings leave out documents that do not fit the model, as SQLite's do (backend audit C-10).

jsonb always holds valid JSON, but a document written by a newer release (after a downgrade) or edited by hand may not
fit the model: ``list``, ``find``, ``items`` and ``list_prefix`` leave it out and report it in ``store.damaged`` instead
of failing for every document. Reading it alone (``get``) still raises. Needs ``NODESTRO_TEST_POSTGRES``.
"""

from __future__ import annotations

import logging

import postgres_support
from pydantic import BaseModel
import pytest

pytest.importorskip("psycopg")

from nodestro_runtime.storage_postgres import PostgresDatabase


class Run(BaseModel):
    id: str
    status: str
    attempts: int = 0


def test_a_document_that_does_not_fit_is_left_out_and_reported(caplog: pytest.LogCaptureFixture) -> None:
    postgres_support.require_postgres()
    schema = postgres_support.new_schema()
    database = PostgresDatabase(postgres_support.DSN, schema=schema)
    try:
        database.migrate()
        store = database.store("acme")
        store.put("runs", "run_ok", Run(id="run_ok", status="running"))
        store.put_raw("runs", "run_newer", {"id": "run_newer", "status": "running", "attempts": "many"})
        with caplog.at_level(logging.WARNING):
            assert [run.id for run in store.list("runs", Run)] == ["run_ok"]
            assert [run.id for run in store.find("runs", "status", ["running"], Run)] == ["run_ok"]
            assert [key for key, _run in store.items("runs", Run)] == ["run_ok"]
            assert [run.id for run in store.list_prefix("runs", "run_", Run)] == ["run_ok"]
        assert set(store.damaged.found) == {("runs", "run_newer")}
        assert sum("Damaged document runs/run_newer" in record.message for record in caplog.records) == 1
        with pytest.raises(ValueError):
            store.get("runs", "run_newer", Run)
    finally:
        database.close()
        postgres_support.drop_schema(schema)
