"""The core runtime does not import the MCP client, so a copy without it (an exported project) still works."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "src" / "nodestro_runtime"
#: Modules allowed to import the client: the workspace attaches it.
ATTACHES = {"workspace.py"}


def _runtime_imports(path: Path) -> set[str]:
    found: set[str] = set()

    class Visitor(ast.NodeVisitor):
        def visit_If(self, node: ast.If) -> None:
            if isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
                for item in node.orelse:
                    self.visit(item)
                return
            self.generic_visit(node)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            module = node.module or ""
            if (node.level == 1 and module.split(".")[0] == "mcp") or module.startswith("nodestro_runtime.mcp."):
                found.add(module)

    Visitor().visit(ast.parse(path.read_text(encoding="utf-8")))
    return found


@pytest.mark.parametrize("path", sorted(SOURCE.glob("*.py")), ids=lambda path: path.name)
def test_core_modules_do_not_import_the_mcp_client(path: Path) -> None:
    if path.name in ATTACHES:
        return
    assert not _runtime_imports(path), path.name
