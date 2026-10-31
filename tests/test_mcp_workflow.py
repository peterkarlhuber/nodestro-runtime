"""MCP tools in workflow runs: agents call approved tools, the run records the provenance, policy holds (WP-1/2)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")

from mcp_support import LoopbackServer, serve

from nodestro_runtime.mcp.models import McpServerIn
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.schemas import RunIn, WorkflowIn
from nodestro_runtime.testing import ScriptedClient, text, tool_use
from nodestro_runtime.workspace import Workspace

TOOL = "mcp__catalog__search"


@pytest.fixture
def server() -> Iterator[LoopbackServer]:
    handle = serve()
    yield handle
    handle.stop()


def flow(tools: list[str]) -> WorkflowIn:
    return WorkflowIn.model_validate(
        {
            "name": "MCP",
            "nodes": [
                {"id": "start", "type": "start"},
                {
                    "id": "a",
                    "type": "agent",
                    "label": "A",
                    "agent": {"name": "A", "system_prompt": "LOOKUP", "tools": tools},
                },
            ],
            "edges": [{"id": "e", "source": "start", "target": "a"}],
        }
    )


def workspace_with(tmp_path: Path, server: LoopbackServer, script: dict[str, list[Any]]) -> Workspace:
    workspace = Workspace(tmp_path / "data", client_factory=lambda _p: ScriptedClient(script), integrations=[])
    added = workspace.mcp.add(McpServerIn(name="Catalog", url=server.url, allow_loopback=True), actor="admin")
    workspace.mcp.discover(added.id)
    workspace.mcp.decide(added.id, "search", approve=True, access="read", actor="admin")
    return workspace


async def run(workspace: Workspace, workflow: WorkflowIn, trigger: str = "manual") -> Any:
    saved = workspace.save_workflow(workflow)
    started = await workspace.run(saved, RunIn(input="x"), trigger=trigger)  # type: ignore[arg-type]
    return await workspace.wait(started.id, timeout=30)


async def test_an_agent_calls_an_approved_mcp_tool(tmp_path: Path, server: LoopbackServer) -> None:
    workspace = workspace_with(tmp_path, server, {"LOOKUP": [tool_use(TOOL, {"query": "q1"}), text("found it")]})
    try:
        assert TOOL in [info.name for info in workspace.tools.all() if info.kind == "mcp"]
        done = await run(workspace, flow([TOOL]))
        assert done.status == "succeeded", done.error
        [call] = done.nodes["a"].tool_calls
        assert call.name == TOOL and not call.is_error
        assert "results for q1" in (call.output or "")
        assert call.provenance["source"] == "mcp"
        assert call.provenance["qualified_name"].endswith("/search")
        assert call.provenance["definition_hash"].startswith("sha256:")
        assert call.provenance["external_cost"] == "unknown"
    finally:
        workspace.close()


async def test_unapproved_tools_stop_the_agent_before_it_starts(tmp_path: Path, server: LoopbackServer) -> None:
    workspace = workspace_with(tmp_path, server, {"LOOKUP": [text("never")]})
    try:
        # The pre-check refuses the start: the tool is not approved.
        with pytest.raises(NodestroError) as caught:
            await run(workspace, flow(["mcp__catalog__delete_page"]))
        assert "not approved" in str(caught.value)
        issues = workspace.builder.validate(flow(["mcp__catalog__delete_page"]).nodes[1].agent)  # type: ignore[arg-type]
        assert any(issue.code == "mcp.tool_not_approved" for issue in issues)
    finally:
        workspace.close()


async def test_unattended_runs_need_the_operator_switch(
    tmp_path: Path, server: LoopbackServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NODESTRO_MCP_UNATTENDED", raising=False)
    script = {"LOOKUP": [tool_use(TOOL, {"query": "q"}), text("ok")] * 2}
    workspace = workspace_with(tmp_path, server, script)
    try:
        done = await run(workspace, flow([TOOL]), trigger="schedule")
        assert done.status == "failed"
        assert done.nodes["a"].error_code == "mcp.unattended_not_enabled"
        monkeypatch.setenv("NODESTRO_MCP_UNATTENDED", "1")
        done = await run(workspace, flow([TOOL]), trigger="schedule")
        assert done.status == "succeeded", done.error
    finally:
        workspace.close()


async def test_a_definition_change_between_runs_fails_closed(tmp_path: Path, server: LoopbackServer) -> None:
    script = {"LOOKUP": [tool_use(TOOL, {"query": "q"}), text("after the error")]}
    workspace = workspace_with(tmp_path, server, script)
    try:

        def search(query: str) -> str:
            """Changed description."""
            return "changed"

        server.mcp.remove_tool("search")
        server.mcp.add_tool(search, name="search")
        done = await run(workspace, flow([TOOL]))
        # The call is refused and the model sees the reason; no data reached the changed tool.
        [call] = done.nodes["a"].tool_calls
        assert call.is_error and "mcp.tool_changed" in (call.output or "")
        assert not any(r["method"] == "POST" and "changed" in str(r) for r in server.requests)
        # The next build refuses the tool outright.
        with pytest.raises(NodestroError) as caught:
            await run(workspace, flow([TOOL]))
        assert "changed the definition" in str(caught.value)
    finally:
        workspace.close()
