"""The runtime's place in the platform: what it may import and the extension contract."""

from __future__ import annotations

import ast
from importlib import metadata
from pathlib import Path

from orkexa import Tool
import pytest

from nodestro_runtime.extensions import (
    CONTRACT,
    EXTENSION_TOOLS_ENV,
    ExtensionError,
    Extensions,
    Integration,
    discover,
)
from nodestro_runtime.extensions import _split as split_tools
from nodestro_runtime.tool_library import ToolLibrary
from nodestro_runtime.tool_policy import UNKNOWN, ToolCapability
from nodestro_runtime.tool_spec import ToolSpec
from nodestro_runtime.workspace import Workspace

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


# Security review 2026-11 (phase 0R, S1, S3) and owner decision E-0R-2 A: tools of installed integrations are
# locked; foreign code runs only in the Docker sandbox, which extension tools cannot use yet (N-22), so no switch
# value unlocks them.


class _Ocr:
    name = "fake-ocr"

    def available(self) -> bool:
        return True

    def recognize(self, data: bytes, name: str) -> object:
        raise AssertionError("not called")


def _installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *found: Integration) -> Workspace:
    monkeypatch.setattr("nodestro_runtime.workspace.discover", lambda: list(found))
    return Workspace(tmp_path / "data")


@pytest.mark.parametrize(("switch", "reason"), [(None, "off"), ("0", "off"), ("1", "isolation_unavailable")])
def test_tools_of_installed_integrations_stay_locked_for_every_switch_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, switch: str | None, reason: str
) -> None:
    if switch is None:
        monkeypatch.delenv(EXTENSION_TOOLS_ENV, raising=False)
    else:
        monkeypatch.setenv(EXTENSION_TOOLS_ENV, switch)
    pack = Integration(name="pack", tools=[_tool("echo_text")], ocr=_Ocr())  # type: ignore[arg-type]
    with _installed(tmp_path, monkeypatch, pack) as workspace:
        assert "echo_text" not in workspace.tools.builtin
        assert workspace.extensions.tools() == [] and workspace.extensions.specs() == {}
        assert workspace.extensions.locked == {"echo_text": reason}
        assert workspace.extensions.summary()[0]["locked"] == {"echo_text": reason}
        # The integration's OCR engine is not a tool and stays usable.
        assert workspace.extensions.ocr() is pack.ocr


def test_integrations_passed_explicitly_keep_their_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(EXTENSION_TOOLS_ENV, raising=False)
    with Workspace(tmp_path / "data", integrations=[Integration(name="own", tools=[_tool("own_tool")])]) as workspace:
        assert "own_tool" in workspace.tools.builtin
        assert workspace.extensions.locked == {}


def test_an_installed_integration_cannot_state_its_own_capability() -> None:
    read_only = ToolSpec(
        id="sneaky",
        category="web",
        capability=ToolCapability(access="read", reach="internal", source="author"),
        offline=True,
    )
    pack = Integration(name="pack", tools=[(read_only, _tool("sneaky"))])
    assert split_tools(pack, installed=True)[0][0].capability == UNKNOWN  # type: ignore[union-attr]
    # The embedding application's own integration keeps the stated capability, marked as the author's.
    assert split_tools(pack)[0][0].capability.access == "read"  # type: ignore[union-attr]
