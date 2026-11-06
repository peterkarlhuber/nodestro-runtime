"""The catalog of problem codes the runtime reports (backend audit X-03, RT-011).

Every error, validation issue, warning and status message carries a stable ``code`` (see :mod:`.problems`) that the
studio turns into a sentence in the user's language. This module keeps the machine-readable list of those codes and
the names of their parameters, so that a new code cannot reach a user interface unnoticed:

- :func:`catalog` reads the list shipped with the package (``error_codes.json``); the HTTP API publishes it together
  with its own codes (nodestro-api API-016).
- :func:`scan` finds the codes in the runtime's source: the first argument (or ``code=``) of every call that builds
  a problem (``NodestroError``, ``problem``, ``issue`` ...) with the names of its keyword arguments, and every other
  string constant in one of those namespaces (codes kept in tables, such as ``{"rejected": "tool.approval_rejected"}``).
  Codes put together at run time are listed by :func:`dynamic_codes` (Orkexa's error classes, the local model
  server's error codes, the resume reasons by step status).
- ``tests/test_error_codes.py`` fails when the source and the catalog differ; ``python -m
  nodestro_runtime.error_codes --write`` writes the catalog again (and ``--check`` reports a difference).

The scan is a safety net, not a parser of every possibility: a code built from parts that are not listed in
:func:`dynamic_codes` is not found.
"""

from __future__ import annotations

import argparse
import ast
from collections.abc import Iterable
from functools import cache
import json
from pathlib import Path
import re
import sys
from typing import get_args

#: A problem code: lower-case namespace, a dot, then name parts (``run.agent.MaxToolRoundsError`` keeps the class).
CODE = re.compile(r"[a-z][a-z0-9_]*(?:\.[A-Za-z0-9_]+)+")
#: Calls whose first argument (or ``code=``) is a problem code.
_BUILDERS = re.compile(r"(?:Error|Problem|problem|issue|Issue)$")
#: Keyword arguments of those calls that are not parameters of the code.
_NOT_PARAMS = frozenset({"message", "level", "field"})
#: Constants in a code namespace that are not codes: file names, template placeholders, prefixes of a code that is
#: completed at run time (listed in :func:`dynamic_codes` instead).
NOT_CODES = frozenset(
    {
        "document.docx",
        "run.files",
        "run.input",
        "run.values",
        "secrets.json",
        "tool.py",
        "workflow.input",
    }
)
_PACKAGE = Path(__file__).resolve().parent
_CATALOG = _PACKAGE / "error_codes.json"


@cache
def catalog() -> dict[str, list[str]]:
    """Every code the runtime reports, with the names of its parameters (sorted), as shipped with the package."""
    data = json.loads(_CATALOG.read_text(encoding="utf-8"))
    return {str(code): [str(name) for name in names] for code, names in data["codes"].items()}


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _code_of(node: ast.Call) -> str | None:
    candidates = [*node.args[:1], *(keyword.value for keyword in node.keywords if keyword.arg == "code")]
    for value in candidates:
        if isinstance(value, ast.Constant) and isinstance(value.value, str) and CODE.fullmatch(value.value):
            return value.value
    return None


def _python_files(root: Path) -> Iterable[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def scan(root: Path = _PACKAGE, *, exclude: Iterable[str] = NOT_CODES) -> dict[str, set[str]]:
    """The codes in the Python source under ``root`` with the parameter names seen with them (see the module)."""
    excluded = set(exclude)
    found: dict[str, set[str]] = {}
    trees = [ast.parse(path.read_text(encoding="utf-8"), filename=str(path)) for path in _python_files(root)]
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _BUILDERS.search(_call_name(node)):
                code = _code_of(node)
                if code is not None and code not in excluded:
                    params = found.setdefault(code, set())
                    params.update(
                        keyword.arg for keyword in node.keywords if keyword.arg and keyword.arg not in _NOT_PARAMS
                    )
                    params.discard("code")
    spaces = {code.split(".", 1)[0] for code in found}
    for tree in trees:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and CODE.fullmatch(node.value)
                and node.value.split(".", 1)[0] in spaces
                and node.value not in excluded
                and not node.value.endswith("_")  # the start of a code completed at run time
            ):
                found.setdefault(node.value, set())
    return found


def _orkexa_errors() -> list[str]:
    """The names of Orkexa's error classes (subclasses of ``OrkexaError``), read from its source, so the result does
    not depend on which of its modules happen to be imported."""
    import orkexa

    bases: dict[str, set[str]] = {}
    for path in _python_files(Path(orkexa.__file__).resolve().parent):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef):
                names = {base.id if isinstance(base, ast.Name) else getattr(base, "attr", "") for base in node.bases}
                bases.setdefault(node.name, set()).update(names)
    found = {"OrkexaError"}
    while True:
        more = {name for name, parents in bases.items() if parents & found} - found
        if not more:
            break
        found |= more
    return sorted(found - {"OrkexaError"})


def _ollama_codes() -> list[str]:
    import orkexa.local

    pattern = re.compile(r"OllamaError\(\s*\"([a-z_]+)\"")
    codes: set[str] = set()
    for path in _python_files(Path(orkexa.local.__file__).resolve().parent):
        codes.update(pattern.findall(path.read_text(encoding="utf-8")))
    return sorted(codes)


def dynamic_codes() -> dict[str, set[str]]:
    """The codes the runtime puts together at run time: ``run.agent.<Orkexa error class>`` (``engine.node_problem``,
    parameter ``type``), ``local.<Ollama error code>`` (``local_service._error``, parameter ``status``) and
    ``resume.was_<step status>`` (``graph``: the reason a step runs again)."""
    from .schemas import NodeStatus

    found: dict[str, set[str]] = {f"run.agent.{name}": {"type"} for name in _orkexa_errors()}
    found.update({f"local.{code}": {"status"} for code in _ollama_codes()})
    found.update(
        {f"resume.was_{status}": set() for status in get_args(NodeStatus) if status not in ("succeeded", "skipped")}
    )
    # Parameters added to a code after it was built (engine.tool_rounds_problem, RT-067).
    found["run.agent.MaxToolRoundsError"].update({"rounds", "calls", "repeated", "tool", "looping", "local"})
    return found


def collect(root: Path = _PACKAGE) -> dict[str, list[str]]:
    """What the catalog should hold now: the scanned and the dynamic codes, merged, sorted."""
    merged: dict[str, set[str]] = {}
    for part in (scan(root), dynamic_codes()):
        for code, params in part.items():
            merged.setdefault(code, set()).update(params)
    return {code: sorted(merged[code]) for code in sorted(merged)}


def render(codes: dict[str, list[str]]) -> str:
    """The catalog file: one code per line, so a change shows as the lines of the codes it adds or removes."""
    lines = [f"  {json.dumps(code)}: {json.dumps(params)}" for code, params in codes.items()]
    description = json.dumps(
        "Problem codes of nodestro-runtime with the names of their parameters "
        "(generated by python -m nodestro_runtime.error_codes --write)."
    )
    return "{\n" + f' "description": {description},\n "codes": {{\n' + ",\n".join(lines) + "\n }\n}\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--write", action="store_true", help="write error_codes.json from the source")
    action.add_argument("--check", action="store_true", help="exit 1 when error_codes.json differs from the source")
    args = parser.parse_args(argv)
    text = render(collect())
    if args.write:
        _CATALOG.write_text(text, encoding="utf-8")
        catalog.cache_clear()
        return 0
    if args.check:
        current = _CATALOG.read_text(encoding="utf-8") if _CATALOG.is_file() else ""
        if current != text:
            print(
                "error_codes.json is out of date: run python -m nodestro_runtime.error_codes --write", file=sys.stderr
            )
            return 1
        return 0
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
