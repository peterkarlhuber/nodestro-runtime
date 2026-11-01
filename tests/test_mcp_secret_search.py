"""Independent review L-8: a server token never shows up anywhere the platform writes.

After a full use (connect, discover, approve, a workflow run that calls the tool) the token is searched in the log
output, the exported run trace, every file of the data folder (SQLite database, ``-wal`` and ``-shm`` files) and,
with ``NODESTRO_TEST_POSTGRES``, in every row of every table of the tenant's PostgreSQL schema.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
import logging
import os
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")

from mcp_support import LoopbackServer, serve

from nodestro_runtime.mcp.models import McpServerIn, ToolDefinition
from nodestro_runtime.mcp.vault import VAULT_KEY_ENV
from nodestro_runtime.schemas import RunIn, WorkflowIn
from nodestro_runtime.testing import ScriptedClient, text, tool_use
from nodestro_runtime.workspace import Workspace

TOKEN = "l8-Zq7vXw2Rt9Kp4Ls6Nd"
TOOL = "mcp__catalog__search"


@pytest.fixture
def server() -> Iterator[LoopbackServer]:
    handle = serve(token=TOKEN)
    yield handle
    handle.stop()


def _flow() -> WorkflowIn:
    return WorkflowIn.model_validate(
        {
            "name": "L8",
            "nodes": [
                {"id": "start", "type": "start"},
                {
                    "id": "a",
                    "type": "agent",
                    "label": "A",
                    "agent": {"name": "A", "system_prompt": "LOOKUP", "tools": [TOOL]},
                },
            ],
            "edges": [{"id": "e", "source": "start", "target": "a"}],
        }
    )


def _files_holding(root: Path, needle: bytes) -> list[str]:
    found = []
    for path in root.rglob("*"):
        if path.is_file() and needle in path.read_bytes():
            found.append(str(path.relative_to(root)))
    return found


async def test_the_token_is_nowhere_in_logs_traces_or_data_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, server: LoopbackServer
) -> None:
    monkeypatch.setenv(VAULT_KEY_ENV, base64.b64encode(os.urandom(32)).decode())
    caplog.set_level(logging.DEBUG)
    data = tmp_path / "data"
    script: dict[str, list[Any]] = {"LOOKUP": [tool_use(TOOL, {"query": "q1"}), text("found it")]}
    workspace = Workspace(data, client_factory=lambda _p: ScriptedClient(script), integrations=[])
    try:
        added = workspace.mcp.add(
            McpServerIn(name="Catalog", url=server.url, allow_loopback=True, auth="bearer", token=TOKEN), actor="admin"
        )
        assert workspace.mcp.server(added.id).token_storage == "vault"
        workspace.mcp.discover(added.id)
        tool = workspace.mcp.tool(added.id, "search")
        workspace.mcp.decide(added.id, "search", approve=True, expected_hash=tool.hash, access="read", actor="admin")
        saved = workspace.save_workflow(_flow())
        started = await workspace.run(saved, RunIn(input="x"), trigger="manual")
        done = await workspace.wait(started.id, timeout=30)
        assert done.status == "succeeded", done.error
        assert any(r["headers"].get("authorization") == f"Bearer {TOKEN}" for r in server.requests)  # it was used
        # The exported trace (the run as JSON) and the API views.
        assert TOKEN not in done.model_dump_json()
        assert TOKEN not in workspace.mcp.view(workspace.mcp.server(added.id)).model_dump_json()
        assert TOKEN not in str([event.model_dump() for event in workspace.mcp.events()])
        # The data folder while the database is open (write-ahead log still present) ...
        assert _files_holding(data, TOKEN.encode()) == []
    finally:
        workspace.close()
    # ... and after it was checkpointed and closed.
    assert _files_holding(data, TOKEN.encode()) == []
    assert TOKEN not in caplog.text


def test_the_token_is_nowhere_in_the_postgres_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import postgres_support

    pytest.importorskip("psycopg")
    import psycopg
    from psycopg import sql

    from nodestro_runtime.postgres_tables import tenant_storage
    from nodestro_runtime.server_settings import SecretBox
    from nodestro_runtime.storage_postgres import PostgresDatabase

    dsn = postgres_support.require_postgres()
    schema = postgres_support.new_schema()
    database = PostgresDatabase(dsn, schema=schema)
    database.migrate()
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("NODESTRO_MCP_ENDPOINTS", "https://mcp.example.com")
    workspace = Workspace(
        tmp_path / "alpha",
        integrations=[],
        storage=tenant_storage(database, "alpha", secrets=SecretBox(os.urandom(32))),
    )
    try:
        added = workspace.mcp.add(
            McpServerIn(name="CRM", url="https://mcp.example.com/mcp", auth="bearer", token=TOKEN), actor="admin"
        )
        assert added.token_storage == "vault"
        workspace.mcp.apply_discovery(added, [ToolDefinition(name="lookup", description="Find a customer")])
        tool = workspace.mcp.tool(added.id, "lookup")
        workspace.mcp.decide(added.id, "lookup", approve=True, expected_hash=tool.hash, access="read", actor="admin")
        workspace.mcp.update(
            added.id, McpServerIn(name="CRM", url="https://mcp.example.com/mcp", auth="bearer", token=TOKEN + "x")
        )
        assert workspace.mcp.token(workspace.mcp.server(added.id)) == TOKEN + "x"
        with psycopg.connect(dsn) as connection:
            tables = [
                row[0]
                for row in connection.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (schema,)
                )
            ]
            assert tables
            hits = []
            for table in tables:
                query = sql.SQL("SELECT t::text FROM {}.{} AS t").format(sql.Identifier(schema), sql.Identifier(table))
                for (row,) in connection.execute(query):
                    if TOKEN in row:
                        hits.append(table)
            assert hits == []
    finally:
        workspace.close()
        database.close()
        postgres_support.drop_schema(schema)
    assert TOKEN not in caplog.text
