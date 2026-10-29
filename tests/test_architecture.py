"""The runtime's place in the platform: what it may import and the extension contract."""

from __future__ import annotations

import ast
from importlib import metadata
from pathlib import Path

from orkexa import Tool
import pytest

from nodestro_runtime.extensions import CONTRACT, ExtensionError, Extensions, Integration, discover
from nodestro_runtime.tool_library import ToolLibrary

SOURCE = Path(__file__).resolve().parents[1] / "src" / "nodestro_runtime"
#: Packages the runtime must never import (dependency rules of the platform).
FORBIDDEN = {
    "nodestro_api",
    "nodestro_studio",
    "nodestro_examples",
    "nodestro_integrations",
    "nodestro_client",
    "nodestro",
    "claude_agent",
    "fastapi",
    "starlette",
    "uvicorn",
}


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


@pytest.mark.parametrize("path", sorted(SOURCE.glob("*.py")), ids=lambda path: path.name)
def test_the_runtime_imports_no_server_studio_or_former_library(path: Path) -> None:
    assert not (_imports(path) & FORBIDDEN), path.name


def _tool(name: str) -> Tool:
    def run(value: str) -> str:
        """Echo.

        Args:
            value: Any text.

        Returns:
            The same text.
        """
        return value

    return Tool(run, name=name)


def test_integrations_add_trusted_tools_to_the_library(tmp_path: Path) -> None:
    extensions = Extensions([Integration(name="pack", tools=[_tool("echo_text")])])
    library = ToolLibrary(tmp_path / "tools", extra_tools=extensions.tools())
    assert "echo_text" in library.builtin and library.get("echo_text").kind == "builtin"
    assert "calculator" in library.builtin


def test_integrations_must_match_the_contract() -> None:
    with pytest.raises(ExtensionError) as wrong:
        Extensions([Integration(name="old", contract=CONTRACT + 1)])
    assert wrong.value.code == "extension.contract"
    with pytest.raises(ExtensionError) as duplicate:
        Extensions([Integration(name="a", tools=[_tool("same")]), Integration(name="b", tools=[_tool("same")])])
    assert duplicate.value.code == "extension.tool_duplicate"


def test_discovery_skips_broken_entry_points() -> None:
    good = metadata.EntryPoint(name="good", value="tests_helpers:good", group="nodestro.integrations")
    broken = metadata.EntryPoint(name="broken", value="does_not_exist:thing", group="nodestro.integrations")

    class Loaded(metadata.EntryPoint):
        def load(self) -> object:
            return lambda: Integration(name="good")

    found = discover(lambda: [Loaded(good.name, good.value, good.group), broken])
    assert [item.name for item in found] == ["good"]
