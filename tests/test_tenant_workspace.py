"""Workspaces of different tenants on one PostgreSQL database (needs ``NODESTRO_TEST_POSTGRES``)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import postgres_support
import pytest

from nodestro_runtime.problems import NodestroError
from nodestro_runtime.schemas import WorkflowIn
from nodestro_runtime.workspace import LOCAL_TENANT, Workspace

pytest.importorskip("psycopg")

from nodestro_runtime.postgres_tables import tenant_storage
from nodestro_runtime.storage_postgres import PostgresDatabase


@pytest.fixture
def database() -> Iterator[PostgresDatabase]:
    dsn = postgres_support.require_postgres()
    schema = postgres_support.new_schema()
    opened = PostgresDatabase(dsn, schema=schema)
    opened.migrate()
    yield opened
    opened.close()
    postgres_support.drop_schema(schema)


def _workspace(database: PostgresDatabase, root: Path, tenant: str) -> Workspace:
    return Workspace(root / tenant, integrations=[], storage=tenant_storage(database, tenant))


def test_each_tenant_sees_only_its_own_data(database: PostgresDatabase, tmp_path: Path) -> None:
    alpha, beta = _workspace(database, tmp_path, "alpha"), _workspace(database, tmp_path, "beta")
    try:
        assert (alpha.tenant_id, beta.tenant_id) == ("alpha", "beta")
        saved = alpha.save_workflow(WorkflowIn.model_validate({"name": "Alpha only", "nodes": [], "edges": []}))
        assert [item.id for item in alpha.store.list("workflows", type(saved))] == [saved.id]
        assert beta.store.list("workflows", type(saved)) == []
        with pytest.raises(KeyError):
            beta.workflow(saved.id)
        alpha.knowledge.add(b"Alpha secret contract", name="alpha.txt")
        assert beta.knowledge.files() == [] and beta.knowledge.search("secret") == []
        alpha.memory.open("Team").write("/memories/a.md", "alpha")
        assert beta.memory.all() == []
        assert alpha.data_dir != beta.data_dir  # files of each tenant in their own area
    finally:
        alpha.close()
        beta.close()
    assert database.closed is False  # the workspaces do not own the server database


def test_a_tenant_workspace_has_no_sqlite_file(database: PostgresDatabase, tmp_path: Path) -> None:
    workspace = _workspace(database, tmp_path, "alpha")
    try:
        with pytest.raises(NodestroError) as caught:
            workspace.sqlite_store  # noqa: B018
        assert caught.value.code == "storage.not_sqlite"
        assert not (tmp_path / "alpha" / "nodestro.db").exists()
    finally:
        workspace.close()


def test_the_desktop_is_the_local_tenant(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "desktop", integrations=[])
    try:
        assert workspace.tenant_id == LOCAL_TENANT
        assert workspace.sqlite_store is workspace.store
    finally:
        workspace.close()
