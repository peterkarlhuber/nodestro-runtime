"""The agent test follows the per-call tool rules for own tools too (independent review H-1, S-2 review of #60).

With per-call approvals on, an own tool that changes data is refused in a test (``tool.approval_not_in_test``) and its
code does not run; a reading tool runs; the agent's memory tool is not asked about (like in a run, it writes only to
the test's ephemeral copy).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nodestro_runtime.agent_test import AgentTestIn
from nodestro_runtime.schemas import AgentConfig, MemoryConfig
from nodestro_runtime.testing import ScriptedClient, text, tool_use
from nodestro_runtime.tool_approvals import ENABLE_ENV
from nodestro_runtime.tool_library import CUSTOM_TESTS_TEMPLATE, CUSTOM_TOOL_TEMPLATE
from nodestro_runtime.workspace import Workspace

CUSTOM = "celsius_to_fahrenheit"


def _workspace(tmp_path: Path, script: dict[str, Any]) -> Workspace:
    return Workspace(tmp_path / "data", client_factory=lambda _p: ScriptedClient(script), integrations=[])


def _with_custom_tool(workspace: Workspace) -> None:
    result, info = workspace.tools.save_custom(
        CUSTOM_TOOL_TEMPLATE,
        CUSTOM_TESTS_TEMPLATE,
        CUSTOM,
        trust=workspace.tools.editor_trust(CUSTOM, CUSTOM_TOOL_TEMPLATE),
    )
    assert result.ok and info is not None, result
    assert workspace.tools.get(CUSTOM).capability.access == "write"


def test_an_own_tool_that_changes_data_is_refused_and_a_reading_tool_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENABLE_ENV, "1")
    script = {
        "LOOKUP": [tool_use(CUSTOM, {"celsius": 100}), tool_use("calculator", {"expression": "6*7"}), text("done")]
    }
    workspace = _workspace(tmp_path, script)
    try:
        _with_custom_tool(workspace)
        assert workspace.runs.tool_gate is not None
        agent = AgentConfig(name="A", system_prompt="LOOKUP", tools=[CUSTOM, "calculator"])
        result, _tracker = workspace.tester.run(AgentTestIn(agent=agent, input="x"))
        custom = next(c for c in result.tool_calls if c.name == CUSTOM)
        assert custom.approval is not None and custom.approval.status == "denied"
        assert custom.approval.code == "tool.approval_not_in_test"
        assert "212" not in (custom.output or "")  # the tool code did not run
        calculator = next(c for c in result.tool_calls if c.name == "calculator")
        assert calculator.approval is not None and calculator.approval.status == "allowed"
        assert not calculator.is_error and "42" in (calculator.output or "")
    finally:
        workspace.close()


def test_without_approvals_the_own_tool_runs_as_before(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENABLE_ENV, raising=False)
    workspace = _workspace(tmp_path, {"LOOKUP": [tool_use(CUSTOM, {"celsius": 100}), text("done")]})
    try:
        _with_custom_tool(workspace)
        assert workspace.runs.tool_gate is None
        agent = AgentConfig(name="A", system_prompt="LOOKUP", tools=[CUSTOM])
        result, _tracker = workspace.tester.run(AgentTestIn(agent=agent, input="x"))
        [call] = result.tool_calls
        assert call.approval is None and call.output == "212.0"
    finally:
        workspace.close()


def test_the_memory_tool_is_not_asked_about_in_a_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENABLE_ENV, "1")
    create = tool_use("memory", {"command": "create", "path": "/memories/p.md", "file_text": "Peter"})
    workspace = _workspace(tmp_path, {"MEM": [create, text("ok")]})
    try:
        assert workspace.runs.tool_gate is not None
        agent = AgentConfig(name="A", system_prompt="MEM", memory=MemoryConfig(mode="run"))
        result, _tracker = workspace.tester.run(AgentTestIn(agent=agent, input="x"))
        assert result.status == "succeeded", result.error
        [call] = result.tool_calls
        assert call.name == "memory" and call.approval is None and not call.is_error, call
    finally:
        workspace.close()
