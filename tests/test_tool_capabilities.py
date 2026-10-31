"""Every tool in the catalog carries its capability statement (S-1a, decision D10)."""

from __future__ import annotations

from pathlib import Path

from orkexa.dynamic import SubprocessSandbox

from nodestro_runtime.catalog import SERVER_TOOLS, ServerToolInfo
from nodestro_runtime.mcp.models import McpTool, ToolDefinition
from nodestro_runtime.testing import ContainerDouble
from nodestro_runtime.tool_library import CUSTOM_TESTS_TEMPLATE, CUSTOM_TOOL_TEMPLATE, ToolInfo, ToolLibrary
from nodestro_runtime.tool_policy import BUILTIN_CAPABILITIES, UNKNOWN, ToolCapability

NAME = "celsius_to_fahrenheit"


def host() -> SubprocessSandbox:
    return SubprocessSandbox(timeout=15.0, acknowledge_unsafe=True)


def test_built_in_tools_carry_nodestros_statement(tmp_path: Path) -> None:
    library = ToolLibrary(tmp_path / "tools", sandbox=host())
    infos = {info.name: info for info in library.all() if info.kind == "builtin"}
    assert set(BUILTIN_CAPABILITIES) <= set(infos)
    for name, expected in BUILTIN_CAPABILITIES.items():
        assert infos[name].capability == expected


def test_an_own_tool_on_the_host_counts_as_unknown(tmp_path: Path) -> None:
    library = ToolLibrary(tmp_path / "tools", sandbox=host())
    result, info = library.save_custom(
        CUSTOM_TOOL_TEMPLATE, CUSTOM_TESTS_TEMPLATE, NAME, trust=library.editor_trust(NAME, CUSTOM_TOOL_TEMPLATE)
    )
    assert result.ok and info is not None
    assert library.get(NAME).capability == UNKNOWN
    assert info.capability == UNKNOWN


def test_a_generated_tool_in_a_container_reaches_nothing_but_still_writes(tmp_path: Path) -> None:
    library = ToolLibrary(tmp_path / "tools", sandbox=ContainerDouble())
    result, _ = library.save_custom(CUSTOM_TOOL_TEMPLATE, CUSTOM_TESTS_TEMPLATE, NAME, requirement="Convert")
    assert result.ok
    found = library.get(NAME)
    assert found.kind == "generated"
    assert found.capability == ToolCapability(access="write", reach="internal", source="derived")


def test_a_generated_tool_that_may_not_run_here_derives_nothing(tmp_path: Path) -> None:
    library = ToolLibrary(tmp_path / "tools", sandbox=host())
    result, _ = library.save_custom(
        CUSTOM_TOOL_TEMPLATE, CUSTOM_TESTS_TEMPLATE, NAME, requirement="Convert", sandbox=ContainerDouble()
    )
    assert result.ok
    assert library.isolation("untrusted", NAME) in (None, "kernel")
    if library.isolation("untrusted", NAME) is None:
        assert library.get(NAME).capability == UNKNOWN


def test_an_mcp_tool_carries_the_administrators_class_and_always_reaches_out() -> None:
    definition = ToolDefinition(name="search", annotations={"readOnlyHint": True})
    tool = McpTool(
        server_id="srv_1", name="search", tool_name="mcp__docs__search", definition=definition, hash="h", access="read"
    )
    assert tool.capability() == ToolCapability(access="read", reach="external", source="admin")
    assert McpTool(**{**tool.model_dump(), "access": "destructive"}).capability().access == "destructive"


def test_tool_info_without_a_statement_never_looks_harmless() -> None:
    assert ToolInfo(name="mailer", kind="custom", description="").capability == UNKNOWN
    assert ToolInfo(name="calculator", kind="builtin", description="").capability == BUILTIN_CAPABILITIES["calculator"]
    # The JSON keeps the shape API clients already read for MCP tools.
    dumped = ToolInfo(name="mailer", kind="custom", description="").model_dump(mode="json")
    assert dumped["capability"] == {"access": "write", "reach": "external", "source": "default"}


def test_provider_server_tools_say_what_they_do() -> None:
    assert SERVER_TOOLS["web_search"].capability.reach == "external"
    assert SERVER_TOOLS["web_fetch"].capability.reach == "external"
    assert SERVER_TOOLS["code_execution"].capability.access == "write"
    unknown = ServerToolInfo(key="new_tool", label="New", description="", definition={})
    assert unknown.capability == UNKNOWN
