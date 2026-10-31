"""The capability statement of tools (S-1, decision D10): unknown is write + external; nobody upgrades a tool."""

from __future__ import annotations

import pytest

from nodestro_runtime.builtin_tools import BUILTIN_TOOLS
from nodestro_runtime.catalog import SERVER_TOOLS
from nodestro_runtime.tool_policy import (
    BUILTIN_CAPABILITIES,
    UNKNOWN,
    ToolCapability,
    capability_of,
    parse_capability,
    server_tool_capability,
)


def test_every_builtin_and_provider_tool_has_a_statement() -> None:
    assert {tool.name for tool in BUILTIN_TOOLS} == set(BUILTIN_CAPABILITIES)
    for key in SERVER_TOOLS:
        assert server_tool_capability(key).source == "nodestro"
    assert capability_of(name="calculator", kind="builtin") == ToolCapability(
        access="read", reach="internal", source="nodestro"
    )
    # Web tools read, but reach outside: their queries can carry data out.
    assert server_tool_capability("web_fetch").reach == "external"
    assert server_tool_capability("something_new") == UNKNOWN


def test_without_a_statement_a_tool_writes_and_reaches_out() -> None:
    assert ToolCapability(access="write", reach="external", source="default") == UNKNOWN
    for kind in ("custom", "generated"):
        assert capability_of(name="mailer", kind=kind) == UNKNOWN
    # A tool an extension adds is trusted code, but nobody said what it does.
    assert capability_of(name="send_mail", kind="builtin") == UNKNOWN
    # Unreadable statements count as missing.
    assert capability_of(name="x", kind="custom", stated={"access": "harmless"}) == UNKNOWN
    assert parse_capability("read") is None


def test_the_statement_keeps_who_made_it() -> None:
    stated = {"access": "read", "reach": "external", "source": "nodestro"}
    # A stored statement cannot claim to come from Nodestro.
    assert capability_of(name="lookup", kind="custom", stated=stated).source == "author"
    mcp = capability_of(name="mcp__crm__find", kind="mcp", stated={**stated, "reach": "internal"})
    assert mcp == ToolCapability(access="read", reach="external", source="admin")
    assert capability_of(name="mcp__crm__delete", kind="mcp") == UNKNOWN.model_copy(update={"source": "default"})


@pytest.mark.parametrize(
    ("isolation", "reach", "source"),
    [
        ("container", "internal", "derived"),
        ("kernel", "internal", "derived"),
        ("none", "external", "default"),
        (None, "external", "default"),
    ],
)
def test_reach_is_derived_from_a_sandbox_without_network(isolation: str | None, reach: str, source: str) -> None:
    found = capability_of(name="calc_tax", kind="generated", isolation=isolation)  # type: ignore[arg-type]
    assert (found.access, found.reach, found.source) == ("write", reach, source)
    # MCP tools are always external, whatever runs Nodestro's own code.
    assert capability_of(name="mcp__a__b", kind="mcp", isolation="container").reach == "external"  # type: ignore[arg-type]


def test_statements_are_immutable() -> None:
    with pytest.raises(ValueError):
        UNKNOWN.access = "read"  # type: ignore[misc]


def test_a_statement_without_a_source_takes_the_given_one() -> None:
    found = parse_capability({"access": "read", "reach": "internal"}, source="author")
    assert found == ToolCapability(access="read", reach="internal", source="author")
    assert parse_capability({"access": "read", "reach": "internal"}) is None
