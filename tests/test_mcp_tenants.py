"""MCP on a server: tenant isolation and the operator's egress list (needs ``NODESTRO_TEST_POSTGRES``)."""

from __future__ import annotations

from collections.abc import Iterator
import os
from pathlib import Path

import postgres_support
import pytest

from nodestro_runtime.mcp.models import SERVERS, McpServerIn, ToolDefinition
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.server_settings import SecretBox
from nodestro_runtime.workspace import Workspace

pytest.importorskip("psycopg")

from nodestro_runtime.postgres_tables import tenant_storage  # noqa: E402
from nodestro_runtime.storage_postgres import PostgresDatabase  # noqa: E402

ENDPOINT = "https://mcp.example.com"


@pytest.fixture
def database() -> Iterator[PostgresDatabase]:
    dsn = postgres_support.require_postgres()
    schema = postgres_support.new_schema()
    opened = PostgresDatabase(dsn, schema=schema)
    opened.migrate()
    yield opened
    opened.close()
    postgres_support.drop_schema(schema)


def tenant(database: PostgresDatabase, root: Path, name: str, box: SecretBox) -> Workspace:
    return Workspace(root / name, integrations=[], storage=tenant_storage(database, name, secrets=box))


def test_tenants_never_see_each_others_servers_tools_or_tokens(
    database: PostgresDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NODESTRO_MCP_ENDPOINTS", ENDPOINT)
    box = SecretBox(os.urandom(32))
    alpha, beta = tenant(database, tmp_path, "alpha", box), tenant(database, tmp_path, "beta", box)
    try:
        server = alpha.mcp.add(McpServerIn(name="CRM", url=f"{ENDPOINT}/mcp", auth="bearer", token="alpha-token"))
        assert server.token_storage == "vault"
        alpha.mcp.apply_discovery(server, [ToolDefinition(name="lookup", description="Find a customer")])
        alpha.mcp.decide(server.id, "lookup", approve=True, access="read", actor="alice")
        assert [tool.name for tool in alpha.mcp.usable()] == ["lookup"]
        assert alpha.mcp.token(alpha.mcp.server(server.id)) == "alpha-token"
        # Beta sees nothing and cannot use alpha's tool by name.
        assert beta.mcp.servers() == [] and beta.mcp.all_tools() == [] and beta.mcp.events() == []
        assert "mcp__crm__lookup" not in beta.tools
        with pytest.raises(NodestroError) as caught:
            beta.mcp.check("mcp__crm__lookup")
        assert caught.value.code == "mcp.tool_not_found"
        # A sealed token copied into beta's data does not open there (bound to the tenant).
        sealed = alpha.mcp.server(server.id).token_sealed
        assert sealed and "alpha-token" not in sealed
        beta.store.put_raw(SERVERS, server.id, alpha.store.get_raw(SERVERS, server.id))
        assert beta.mcp.token(beta.mcp.server(server.id)) is None
    finally:
        alpha.close()
        beta.close()


def test_a_server_reaches_only_listed_endpoints_and_never_loopback(
    database: PostgresDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    box = SecretBox(os.urandom(32))
    monkeypatch.delenv("NODESTRO_MCP_ENDPOINTS", raising=False)
    workspace = tenant(database, tmp_path, "alpha", box)
    try:
        with pytest.raises(NodestroError) as caught:
            workspace.mcp.add(McpServerIn(name="CRM", url=f"{ENDPOINT}/mcp"))
        assert caught.value.code == "mcp.endpoint_not_allowed"
        with pytest.raises(NodestroError) as caught:
            workspace.mcp.add(McpServerIn(name="Local", url="http://127.0.0.1:8000/mcp", allow_loopback=True))
        assert caught.value.code == "mcp.https_required"
    finally:
        workspace.close()
