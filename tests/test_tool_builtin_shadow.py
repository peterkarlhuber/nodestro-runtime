"""A stored tool saved before a built-in tool of the same name existed does not stop the workspace (RT3-02)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nodestro_runtime import tool_library
from nodestro_runtime.tool_library import CUSTOM_TESTS_TEMPLATE, CUSTOM_TOOL_TEMPLATE
from nodestro_runtime.tool_policy import BUILTIN_CAPABILITIES
from nodestro_runtime.workspace import Workspace

CODE = CUSTOM_TOOL_TEMPLATE.replace("celsius_to_fahrenheit", "table_totals")
TESTS = CUSTOM_TESTS_TEMPLATE.replace("celsius_to_fahrenheit", "table_totals")


def test_a_stored_tool_with_a_new_built_in_name_is_shadowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    older = tuple(tool for tool in tool_library.BUILTIN_TOOLS if tool.name != "table_totals")
    with monkeypatch.context() as patch:
        patch.setattr(tool_library, "BUILTIN_TOOLS", older)  # the release before table_totals
        with Workspace(data, integrations=[]) as workspace:
            result, _ = workspace.tools.save_custom(
                CODE, TESTS, "table_totals", trust=workspace.tools.editor_trust("table_totals", CODE)
            )
            assert result.ok, result
    with Workspace(data, integrations=[]) as workspace:  # failed with "Tool name already registered" before
        assert workspace.tools.shadowed == {"table_totals"}
        record = workspace.tools.registry.record("table_totals")
        assert record.kind == "trusted"  # the built-in tool, which the tool policy classifies by name
        assert "table_totals" in BUILTIN_CAPABILITIES
    assert any((data / "tools" / "table_totals").glob("v*.json"))  # the stored files are kept
