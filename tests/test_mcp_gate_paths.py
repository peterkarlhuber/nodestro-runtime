"""Every path that runs an MCP tool outside a gated workflow run (independent review H-1).

With per-call approvals on (``NODESTRO_TOOL_APPROVALS=1``) the MCP service accepts tools that change data, because the
run engine asks a person before each such call. The agent test, the tool test panel and releases do not ask, so they
must never run such a call. The first test is the independent reviewer's regression test, taken over unchanged in
substance.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")

from mcp_support import LoopbackServer, serve

from nodestro_runtime.agent_test import AgentTestIn
from nodestro_runtime.mcp.models import McpServerIn
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.schemas import AgentConfig
from nodestro_runtime.testing import ScriptedClient, text, tool_use
from nodestro_runtime.tool_approvals import ENABLE_ENV
from nodestro_runtime.tool_policy import ToolBinding
from nodestro_runtime.workspace import Workspace

DELETE = "mcp__catalog__delete_page"
SEARCH = "mcp__catalog__search"


def _calls_reaching(server: LoopbackServer, tool: str) -> int:
    return len([r for r in server.requests if "tools/call" in str(r) and tool in str(r)])


@pytest.fixture
def server() -> Iterator[LoopbackServer]:
    handle = serve()
    yield handle
    handle.stop()


def _workspace(tmp_path: Path, script: dict[str, Any], server: LoopbackServer) -> Workspace:
    workspace = Workspace(tmp_path / "data", client_factory=lambda _p: ScriptedClient(script), integrations=[])
    added = workspace.mcp.add(McpServerIn(name="Catalog", url=server.url, allow_loopback=True), actor="admin")
    workspace.mcp.discover(added.id)
    tool = workspace.mcp.tool(added.id, "delete_page")
    workspace.mcp.decide(added.id, "delete_page", approve=True, access="write", expected_hash=tool.hash, actor="admin")
    search = workspace.mcp.tool(added.id, "search")
    workspace.mcp.decide(added.id, "search", approve=True, access="read", expected_hash=search.hash, actor="admin")
    return workspace


def test_agent_test_respects_per_call_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: LoopbackServer
) -> None:
    """The reviewer's regression test: a write-classified MCP tool never runs in an agent test."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    workspace = _workspace(tmp_path, {"LOOKUP": [tool_use(DELETE, {"page_id": "7"}), text("done")]}, server)
    try:
        assert workspace.mcp.write_gate is True
        agent = AgentConfig(name="A", system_prompt="LOOKUP", tools=[DELETE])
        result, _tracker = workspace.tester.run(AgentTestIn(agent=agent, input="x"))
        calls = [c for c in result.tool_calls if c.name == DELETE]
        assert calls, result
        assert not any(not c.is_error and "deleted 7" in (c.output or "") for c in calls)
        assert all(c.approval is not None and c.approval.status == "denied" for c in calls)
        assert calls[0].approval is not None and calls[0].approval.code == "tool.approval_not_in_test"
        assert _calls_reaching(server, "delete_page") == 0
    finally:
        workspace.close()


def test_agent_test_follows_the_same_rules_for_read_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: LoopbackServer
) -> None:
    """A read tool with the default binding (``auto``) is allowed without a question, exactly as in a run: it runs
    once, reaches the server once and is marked ``allowed`` (re-review test gap 1: exactly this, nothing else)."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    workspace = _workspace(tmp_path, {"FIND": [tool_use(SEARCH, {"query": "q"}), text("done")]}, server)
    try:
        agent = AgentConfig(name="A", system_prompt="FIND", tools=[SEARCH])
        result, _tracker = workspace.tester.run(AgentTestIn(agent=agent, input="x"))
        assert result.status == "succeeded", result.error
        [call] = result.tool_calls
        assert call.name == SEARCH
        assert call.approval is not None
        assert call.approval.status == "allowed" and call.approval.code is None
        assert call.approval.capability["access"] == "read"
        assert not call.is_error and "results for q" in (call.output or "")
        assert _calls_reaching(server, "search") == 1
    finally:
        workspace.close()


def test_agent_test_refuses_a_read_tool_whose_binding_always_asks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: LoopbackServer
) -> None:
    """The same read tool bound with ``ask: always`` needs a question, which a test cannot ask: refused with
    ``tool.approval_not_in_test``, and the server is never called."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    workspace = _workspace(tmp_path, {"FIND": [tool_use(SEARCH, {"query": "q"}), text("done")]}, server)
    try:
        agent = AgentConfig(
            name="A", system_prompt="FIND", tools=[SEARCH], tool_policies={SEARCH: ToolBinding(ask="always")}
        )
        result, _tracker = workspace.tester.run(AgentTestIn(agent=agent, input="x"))
        [call] = result.tool_calls
        assert call.approval is not None
        assert call.approval.status == "denied" and call.approval.code == "tool.approval_not_in_test"
        assert call.is_error and "results for q" not in (call.output or "")
        assert _calls_reaching(server, "search") == 0
    finally:
        workspace.close()


def test_without_approvals_an_agent_test_refuses_changing_mcp_tools(tmp_path: Path, server: LoopbackServer) -> None:
    workspace = _workspace(tmp_path, {"LOOKUP": [tool_use(DELETE, {"page_id": "7"}), text("done")]}, server)
    try:
        assert workspace.mcp.write_gate is False
        agent = AgentConfig(name="A", system_prompt="LOOKUP", tools=[DELETE])
        result, _tracker = workspace.tester.run(AgentTestIn(agent=agent, input="x"))
        assert result.status == "failed" and result.error_code == "mcp.write_not_supported"
        assert _calls_reaching(server, "delete_page") == 0
    finally:
        workspace.close()


def test_the_tool_test_panel_never_runs_changing_mcp_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: LoopbackServer
) -> None:
    """``ToolLibrary.run`` (the tool editor's test panel, ``POST /api/tools/{name}/run``) does not ask anybody."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    workspace = _workspace(tmp_path, {}, server)
    try:
        assert workspace.mcp.write_gate is True
        with pytest.raises(NodestroError) as caught:
            workspace.tools.run(DELETE, {"page_id": "7"})
        assert caught.value.code == "mcp.write_not_supported"
        output, is_error = workspace.tools.run(SEARCH, {"query": "q"})
        assert not is_error and "results for q" in output
        assert _calls_reaching(server, "delete_page") == 0
    finally:
        workspace.close()


def test_the_process_wide_switch_alone_never_opens_a_changing_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: LoopbackServer
) -> None:
    """Combination check: ``write_gate`` on, but a caller that does not ask (``gated=False``) is refused at build time
    and again at call time, even with a tool object built by a gated caller."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    workspace = _workspace(tmp_path, {}, server)
    try:
        mcp = workspace.mcp
        with pytest.raises(NodestroError, match="workflow runs"):
            mcp.check(DELETE)
        mcp.check(DELETE, gated=True)
        with pytest.raises(NodestroError):
            mcp.call(DELETE, {"page_id": "7"})
        with pytest.raises(NodestroError):
            workspace.tools.tool(DELETE)
        gated_tool: Any = workspace.tools.tool(DELETE, gated=True)
        assert gated_tool.gated is True
        mcp.write_gate = False  # the switch goes off while a gated tool object still exists
        from orkexa import ToolResultError

        with pytest.raises(ToolResultError, match=r"mcp\.write_not_supported"):
            gated_tool.execute({"page_id": "7"})
        assert _calls_reaching(server, "delete_page") == 0
    finally:
        workspace.close()
