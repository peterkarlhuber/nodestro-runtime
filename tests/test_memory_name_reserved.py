"""The name ``memory`` belongs to the agent's system memory tool (independent re-review R-2).

An own or generated tool named ``memory`` could pass for the memory tool and skip the per-call gate. Now the name is
reserved when a tool is saved or generated, a tool stored under it before the reservation is never handed out, and
the gate exempts a call named ``memory`` only when it really reaches the agent's memory backend. The first two tests
are the independent reviewer's tests (``test_rereview_memory_name.py``), adapted to the reservation: saving is
refused, and the stored tool of an older version is written to the registry directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nodestro_runtime.agent_builder import run_own_tool
from nodestro_runtime.agent_test import AgentTestIn
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.schemas import AgentConfig, MemoryConfig, RunIn, WorkflowIn
from nodestro_runtime.testing import ScriptedClient, text, tool_use
from nodestro_runtime.tool_approvals import ENABLE_ENV
from nodestro_runtime.tool_library import CUSTOM_TESTS_TEMPLATE, CUSTOM_TOOL_TEMPLATE
from nodestro_runtime.workspace import Workspace

CODE = CUSTOM_TOOL_TEMPLATE.replace("def celsius_to_fahrenheit", "def memory")
TESTS = CUSTOM_TESTS_TEMPLATE.replace("celsius_to_fahrenheit", "memory")
CALL = tool_use("memory", {"celsius": 100})
CREATE = tool_use("memory", {"command": "create", "path": "/memories/p.md", "file_text": "Peter"})


def _workspace(tmp_path: Path, script: dict[str, Any]) -> Workspace:
    return Workspace(tmp_path / "data", client_factory=lambda _p: ScriptedClient(script), integrations=[])


def _stored_before_the_reservation(workspace: Workspace) -> None:
    """A tool named ``memory`` as an older version could have stored it (checked, tested, approved)."""
    registry = workspace.tools.registry
    record = registry.add_generated(
        name="memory",
        function_name="memory",
        description="Convert Celsius to Fahrenheit.",
        code=CODE,
        tests=TESTS,
        input_schema={"type": "object", "properties": {"celsius": {"type": "number"}}, "required": ["celsius"]},
        output_schema=None,
    )
    registry.approve(record.name, record.version)
    assert "memory" in workspace.tools


def _flow(agent: dict[str, Any]) -> WorkflowIn:
    return WorkflowIn.model_validate(
        {
            "name": "M",
            "nodes": [{"id": "start", "type": "start"}, {"id": "a", "type": "agent", "label": "A", "agent": agent}],
            "edges": [{"id": "e", "source": "start", "target": "a"}],
        }
    )


def test_an_own_tool_cannot_be_saved_as_memory(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, {})
    try:
        result, info = workspace.tools.save_custom(
            CODE, TESTS, "memory", trust=workspace.tools.editor_trust("memory", CODE)
        )
        assert not result.ok and info is None
        assert [e.code for e in result.errors] == ["tool.name_reserved"]
        assert "memory" not in workspace.tools
    finally:
        workspace.close()


def test_a_generated_tool_cannot_be_named_memory() -> None:
    from nodestro_runtime.tool_library import _ReservingToolBuilder

    class Proposal:
        function_name = "memory"

    errors, result = _ReservingToolBuilder._validate(object.__new__(_ReservingToolBuilder), Proposal())
    assert errors == ["'memory' is a reserved tool name; choose another"] and result is None


def test_agent_test_never_runs_a_stored_tool_named_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reviewer's agent test: the stored tool never runs (with or without memory for the agent)."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    workspace = _workspace(tmp_path, {"LOOKUP": [CALL, text("done")]})
    try:
        _stored_before_the_reservation(workspace)
        agent = AgentConfig(name="A", system_prompt="LOOKUP", tools=["memory"])
        assert "config.memory_name_reserved" in [i.code for i in workspace.builder.validate(agent)]
        result, _ = workspace.tester.run(AgentTestIn(agent=agent, input="x"))
        assert result.status == "failed" and result.error_code == "tool.name_reserved"
        assert not any(c.output == "212.0" for c in result.tool_calls)
        with pytest.raises(NodestroError) as caught:
            workspace.tools.run("memory", {"celsius": 100})
        assert caught.value.code == "tool.name_reserved"
    finally:
        workspace.close()


async def test_a_run_never_runs_a_stored_tool_named_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reviewer's run test: the agent with the stored tool does not start, so the tool never runs."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    workspace = _workspace(tmp_path, {"LOOKUP": [CALL, text("done")]})
    try:
        _stored_before_the_reservation(workspace)
        saved = workspace.save_workflow(_flow({"name": "A", "system_prompt": "LOOKUP", "tools": ["memory"]}))
        with pytest.raises(NodestroError) as caught:
            await workspace.run(saved, RunIn(input="x"), trigger="manual")
        assert caught.value.code == "run.agents_invalid" and "reserved" in str(caught.value)
        # Even past the start check (an agent built directly), the library never hands the stored tool out.
        with pytest.raises(NodestroError) as built:
            workspace.tools.tool("memory")
        assert built.value.code == "tool.name_reserved"
    finally:
        workspace.close()


@pytest.mark.parametrize("memory", ["none", "run"])
def test_the_gate_exempts_only_the_real_memory_tool(memory: str) -> None:
    agent = AgentConfig(name="A", system_prompt="x", memory=MemoryConfig(mode=memory))  # type: ignore[arg-type]
    assert run_own_tool(agent, "memory") is (memory == "run")
    assigned = agent.model_copy(update={"tools": ["memory"]})
    assert run_own_tool(assigned, "memory") is False  # a library tool under the name is never exempt
    assert run_own_tool(agent, "calculator") is False


def test_a_library_tool_under_a_run_tool_name_is_not_exempt() -> None:
    """Run tools (input files, knowledge proposals) are exempt only while no library tool took their name."""

    class RunTool:
        name = "read_input_file"

    agent = AgentConfig(name="A", system_prompt="x")
    assert run_own_tool(agent, "read_input_file", [RunTool()]) is True  # type: ignore[list-item]
    taken = agent.model_copy(update={"tools": ["read_input_file"]})
    assert run_own_tool(taken, "read_input_file", [RunTool()]) is False  # type: ignore[list-item]


async def test_the_real_memory_tool_works_unchanged_in_a_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENABLE_ENV, "1")
    workspace = _workspace(tmp_path, {"MEM": [CREATE, text("ok")]})
    try:
        assert workspace.runs.tool_gate is not None
        saved = workspace.save_workflow(_flow({"name": "A", "system_prompt": "MEM", "memory": {"mode": "run"}}))
        started = await workspace.run(saved, RunIn(input="x"), trigger="manual")
        done = await workspace.wait(started.id, timeout=30)
        assert done.status == "succeeded", done.error
        [call] = done.nodes["a"].tool_calls
        assert call.name == "memory" and call.approval is None and not call.is_error, call
    finally:
        workspace.close()


def test_the_real_memory_tool_works_unchanged_in_a_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENABLE_ENV, "1")
    workspace = _workspace(tmp_path, {"MEM": [CREATE, text("ok")]})
    try:
        agent = AgentConfig(name="A", system_prompt="MEM", memory=MemoryConfig(mode="run"))
        result, _tracker = workspace.tester.run(AgentTestIn(agent=agent, input="x"))
        assert result.status == "succeeded", result.error
        [call] = result.tool_calls
        assert call.name == "memory" and call.approval is None and not call.is_error, call
    finally:
        workspace.close()


def test_without_memory_a_call_named_memory_is_gated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An agent without memory that calls ``memory`` (a model may try) goes through the gate like any other name:
    an unknown tool counts as one that changes data, so a test refuses it instead of passing it through."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    workspace = _workspace(tmp_path, {"MEM": [CREATE, text("ok")]})
    try:
        agent = AgentConfig(name="A", system_prompt="MEM")
        result, _tracker = workspace.tester.run(AgentTestIn(agent=agent, input="x"))
        [call] = result.tool_calls
        assert call.approval is not None and call.approval.status == "denied", call
        assert call.is_error
    finally:
        workspace.close()
