"""Tools that another process changes become visible through Orkexa's ``ToolRegistry.refresh()`` (ORK-002), which
replaced the runtime's rebuild of the whole registry (#57): the registry object stays the same, the built-in tools
stay registered, and a stored tool with a built-in name stays hidden behind it."""

from __future__ import annotations

from pathlib import Path

from orkexa.dynamic.tool_registry import ToolRegistry

from nodestro_runtime.tool_library import CUSTOM_TESTS_TEMPLATE, CUSTOM_TOOL_TEMPLATE
from nodestro_runtime.workspace import Workspace

SCHEMA = {"type": "object", "properties": {"celsius": {"type": "number"}}, "required": ["celsius"]}


def _store(path: Path, name: str) -> None:
    """Another process adds and approves a tool in the shared directory."""
    other = ToolRegistry(path)
    record = other.add_generated(
        name=name,
        function_name=name,
        description="Convert Celsius to Fahrenheit.",
        code=CUSTOM_TOOL_TEMPLATE.replace("celsius_to_fahrenheit", name),
        tests=CUSTOM_TESTS_TEMPLATE.replace("celsius_to_fahrenheit", name),
        input_schema=SCHEMA,
        output_schema=None,
    )
    other.approve(record.name, record.version)


def test_a_tool_of_another_process_appears_in_the_same_registry(tmp_path: Path) -> None:
    with Workspace(tmp_path / "data", integrations=[]) as workspace:
        registry = workspace.tools.registry
        assert "convert" not in workspace.tools
        assert registry.path is not None
        _store(registry.path, "convert")
        assert "convert" in workspace.tools
        assert workspace.tools.registry is registry  # refreshed in place, not rebuilt
        assert workspace.tools.registry.record("convert").kind != "trusted"
        assert workspace.tools.registry.record("calculator").kind == "trusted"


def test_a_stored_file_with_a_built_in_name_stays_hidden_after_a_refresh(tmp_path: Path) -> None:
    with Workspace(tmp_path / "data", integrations=[]) as workspace:
        registry = workspace.tools.registry
        assert registry.path is not None and workspace.tools.shadowed == frozenset()
        _store(registry.path, "table_totals")
        assert workspace.tools.registry.record("table_totals").kind == "trusted"
        assert workspace.tools.shadowed == {"table_totals"}
