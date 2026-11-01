"""Independent review L-8: a server token never shows up anywhere the platform writes.

After a full use (connect, discover, approve, a workflow run that calls the tool) the token is searched in the log
output, the exported run trace, every file of the data folder (SQLite database, ``-wal`` and ``-shm`` files) and,
with ``NODESTRO_TEST_POSTGRES``, in every row of every table of the tenant's PostgreSQL schema.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Iterator
import logging
import os
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")

from mcp_support import LoopbackServer, serve

from nodestro_runtime.mcp.models import McpServerIn
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


async def test_the_token_is_nowhere_in_the_postgres_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, server: LoopbackServer
) -> None:
    """The server storage (PostgreSQL tables, the tenant's encrypted secrets) after a real use against a real MCP
    server (re-review test gap 2): connect with a token, discover over the network, approve a reading and a changing
    tool, run a workflow that calls both, ask a person about the changing call, approve it and execute it, then
    change the token. Searched: every row of every table of the tenant schema, the run events streamed to clients,
    the stored MCP events and the approval audit trail, the log output and the exported run trace.

    Only the egress rule is widened for the test server on this computer (a server tenant reaches loopback addresses
    nowhere else); storage, vault, discovery, approval and the call are the server's own."""
    import postgres_support

    pytest.importorskip("psycopg")
    import psycopg
    from psycopg import sql

    from nodestro_runtime.engine import RunManager
    from nodestro_runtime.jobs import JobQueue, QueuedRuns, RunWorker
    from nodestro_runtime.mcp.netguard import EgressPolicy
    from nodestro_runtime.postgres_tables import tenant_storage
    from nodestro_runtime.server_settings import SecretBox
    from nodestro_runtime.storage_postgres import PostgresDatabase
    from nodestro_runtime.tool_approvals import ENABLE_ENV, ToolApprovals, audit_events

    dsn = postgres_support.require_postgres()
    schema = postgres_support.new_schema()
    database = PostgresDatabase(dsn, schema=schema)
    database.migrate()
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv(ENABLE_ENV, "1")
    events: list[str] = []
    emit = RunManager._emit

    def spy(self: RunManager, context: Any, kind: str, node_id: str | None = None, **data: Any) -> Any:
        events.append(f"{kind} {node_id} {data!r}")
        return emit(self, context, kind, node_id, **data)

    monkeypatch.setattr(RunManager, "_emit", spy)
    delete = "mcp__catalog__delete_page"
    script: dict[str, list[Any]] = {
        "LOOKUP": [tool_use(TOOL, {"query": "q1"}), tool_use(delete, {"page_id": "7"}), text("done")]
    }
    box = SecretBox(os.urandom(32))

    worker_db = PostgresDatabase(dsn, schema=schema)  # each process has its own connection

    def tenant(connection: PostgresDatabase) -> Workspace:
        opened = Workspace(
            tmp_path / "alpha",  # the tenant's file folder, shared by its processes like on a server volume
            client_factory=lambda _p: ScriptedClient(script),
            integrations=[],
            storage=tenant_storage(connection, "alpha", secrets=box),
        )
        opened.mcp.hub.policy = EgressPolicy(allowlist=None, require_allowlist=False, loopback_possible=True)
        return opened

    # The API process (it queues runs) and a worker process (it executes them), one tenant, one database.
    workspace = tenant(database)
    worker_space = tenant(worker_db)
    QueuedRuns(JobQueue(database), "alpha", workspace.runs).attach()
    worker = RunWorker(JobQueue(worker_db), lambda _tenant: worker_space, name="w1", poll=0.05, flush=0.05)
    stop = asyncio.Event()
    serving = asyncio.ensure_future(worker.serve(stop))
    try:
        assert worker_space.runs.tool_gate is not None and worker_space.mcp.write_gate is True
        added = workspace.mcp.add(
            McpServerIn(name="Catalog", url=server.url, allow_loopback=True, auth="bearer", token=TOKEN), actor="admin"
        )
        assert added.token_storage == "vault"
        workspace.mcp.discover(added.id)  # over the network, with the token
        for name, access in (("search", "read"), ("delete_page", "write")):
            tool = workspace.mcp.tool(added.id, name)
            workspace.mcp.decide(added.id, name, approve=True, expected_hash=tool.hash, access=access, actor="admin")  # type: ignore[arg-type]
        flow = _flow().model_dump()
        flow["nodes"][1]["agent"]["tools"] = [TOOL, delete]
        saved = workspace.save_workflow(WorkflowIn.model_validate(flow))
        started = await workspace.run(saved, RunIn(input="x"), trigger="manual")
        approvals = ToolApprovals(workspace.store)
        for _ in range(1000):
            if found := approvals.pending(started.id):
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("no approval request")
        [request] = found
        workspace.runs.decide_tool_call(
            started.id, request.id, approved=True, by="operator", arguments=request.arguments_hash
        )
        done = started
        for _ in range(1500):
            current = workspace.runs.get(started.id)
            assert current is not None
            done = current
            if done.status not in ("queued", "running"):
                break
            await asyncio.sleep(0.02)
        assert done.status == "succeeded", done.error
        search, deleted = done.nodes["a"].tool_calls
        assert "results for q1" in (search.output or "") and not search.is_error
        assert deleted.approval is not None and deleted.approval.status == "approved"
        assert "deleted 7" in (deleted.output or "") and not deleted.is_error
        calls = [r for r in server.requests if r["headers"].get("mcp-method") == "tools/call"]
        assert [r["headers"].get("mcp-name") for r in calls] == ["search", "delete_page"]
        assert all(r["headers"].get("authorization") == f"Bearer {TOKEN}" for r in calls)  # the token was used
        workspace.mcp.update(
            added.id,
            McpServerIn(name="Catalog", url=server.url, allow_loopback=True, auth="bearer", token=TOKEN + "x"),
        )
        assert workspace.mcp.token(workspace.mcp.server(added.id)) == TOKEN + "x"
        # Events streamed to clients, stored MCP events, the approval audit trail, the exported trace.
        assert events and any(event.startswith("tool_approval_decided") for event in events)
        assert not [event for event in events if TOKEN in event]
        assert any("deleted 7" in event for event in events)  # control: the search sees the call's events
        assert TOKEN not in str([event.model_dump() for event in workspace.mcp.events()])
        assert TOKEN not in str([event.model_dump() for event in audit_events(workspace.store)])
        assert TOKEN not in done.model_dump_json()
        assert TOKEN not in workspace.mcp.view(workspace.mcp.server(added.id)).model_dump_json()
        with psycopg.connect(dsn) as connection:
            tables = [
                row[0]
                for row in connection.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (schema,)
                )
            ]
            assert tables
            rows = 0
            hits = []
            seen = []  # control: the search sees the run's data (its tool results)
            for table in tables:
                query = sql.SQL("SELECT t::text FROM {}.{} AS t").format(sql.Identifier(schema), sql.Identifier(table))
                for (row,) in connection.execute(query):
                    rows += 1
                    if TOKEN in row:
                        hits.append(table)
                    if "deleted 7" in row:
                        seen.append(table)
            assert hits == []
            assert rows > 0 and seen
        assert _files_holding(tmp_path, TOKEN.encode()) == []
    finally:
        stop.set()
        await asyncio.wait_for(serving, 30)
        workspace.close()
        worker_space.close()
        worker_db.close()
        database.close()
        postgres_support.drop_schema(schema)
    assert TOKEN not in caplog.text
