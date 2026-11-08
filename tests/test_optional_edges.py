"""AC-3 (TABRION QA-18): one blocked-module test per optional edge of nodestro-runtime (edge list K-33, document 13).

The runtime's optional edges (document 13, section 2.2; document 07, AC-3: "nodestro-runtime-Kern ohne axentra (und
ohne tabrion.postgres)"):

* ``axentra``: ``nodestro-runtime[postgres]`` brings Axentra (the platform services of the server profile); without
  it every module imports and ``axentra_platform.available()`` says so, so switching it on is refused;
* ``tabrion.postgres``: the server profile's Tabrion part (``tabrion[postgres]``, MIG-NOD-3..5); no module may need
  it on import, the core uses it only behind a guard (document 05, section 4);
* ``psycopg``: the PostgreSQL driver of the same extra (and of ``orkexa[postgres]``); the desktop and Local Docker
  never install it, so ``storage_postgres`` imports it behind a guard.

Each edge runs once in a fresh ``python -I`` that blocks its modules (``sys.modules[name] = None``) and imports every
module of the package. The probe adds a top-level import of the blocked module to a copy of the package: the same
check must then fail. The meta test keeps the edge list equal to K-33 and to ``pyproject.toml``.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


@dataclass(frozen=True)
class Edge:
    blocked: tuple[str, ...]
    """Modules made unimportable (``sys.modules[name] = None``)."""
    checks: tuple[str, ...]
    """Statements that must hold without the edge (after every module was imported)."""
    probe: tuple[str, str]
    """``(file below src/nodestro_runtime, line)`` the mutation probe adds to a copy of the package."""
    requirement: str
    """The distribution of the ``postgres`` extra that brings the edge (checked by the meta test)."""


EDGES: dict[str, Edge] = {
    "axentra": Edge(
        blocked=("axentra",),
        checks=("from nodestro_runtime.axentra_platform import available\nassert available() is False",),
        probe=("workspace.py", "import axentra"),
        requirement="axentra",
    ),
    "tabrion.postgres": Edge(
        blocked=("tabrion.postgres",),
        checks=(),
        probe=("storage.py", "import tabrion.postgres"),
        requirement="tabrion",
    ),
    "psycopg": Edge(
        blocked=("psycopg", "psycopg_binary", "psycopg_c", "psycopg_pool"),
        checks=(),
        probe=("storage.py", "import psycopg"),
        requirement="psycopg",
    ),
}
"""K-33 edge -> its blocked-module check (exactly one test each)."""

K33_EDGES = frozenset({"axentra", "tabrion.postgres", "psycopg"})
"""The runtime's optional edges: ``nodestro-runtime → axentra`` (optional ``[postgres]``), the ``[postgres]`` part of
``nodestro-runtime → tabrion`` and the PostgreSQL driver of that extra."""


def check_source(edge: Edge) -> str:
    """Block the edge, import every module of the package, run the checks, and refuse a loaded blocked module."""
    tops = tuple(sorted({b for b in edge.blocked if "." not in b}))
    return (
        "import importlib, pkgutil, sys\n"
        f"for _n in {edge.blocked!r}:\n"
        "    sys.modules[_n] = None\n"
        "import nodestro_runtime\n"
        "_names = [m.name for m in pkgutil.walk_packages(nodestro_runtime.__path__, 'nodestro_runtime.')]\n"
        "for _m in _names:\n"
        "    importlib.import_module(_m)\n"
        f"for _s in {edge.checks!r}:\n"
        "    exec(_s, {})\n"
        f"_loaded = sorted(n for n in sys.modules if n.partition('.')[0] in {tops!r} and sys.modules[n])\n"
        "assert not _loaded, _loaded\n"
        "print(len(_names), 'modules', nodestro_runtime.__file__)\n"
    )


def run_python(code: str, path: Path) -> subprocess.CompletedProcess[str]:
    """``python -I -c code`` (no PYTHONPATH, no cwd on sys.path), with ``path`` first on ``sys.path``."""
    code = f"import sys\nsys.path.insert(0, {str(path)!r})\n" + code
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    return subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, env=env, timeout=300, check=False
    )


@pytest.mark.parametrize("edge", sorted(EDGES))
def test_every_module_imports_without_the_edge(edge: str) -> None:
    result = run_python(check_source(EDGES[edge]), SRC)
    assert result.returncode == 0, result.stderr
    assert str(SRC / "nodestro_runtime") in result.stdout  # this checkout, not another installed copy


def _names(requirements: list[str]) -> set[str]:
    return {re.split(r"[\[<>=~!@; ]", r.strip(), maxsplit=1)[0].lower() for r in requirements}


def test_every_k33_edge_has_exactly_one_test() -> None:
    assert set(EDGES) == K33_EDGES
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]
    mandatory = _names(project["dependencies"])
    extra = _names(project["optional-dependencies"]["postgres"])
    assert "axentra" in extra and "axentra" not in mandatory
    assert "psycopg" in extra and "psycopg" not in mandatory
    # tabrion[postgres] arrives with MIG-NOD-3..5 in the extra; a mandatory tabrion (MIG-NOD-6) is fine, its
    # postgres part is not.
    assert not any(re.match(r"tabrion\[[^\]]*postgres", r.replace(" ", "")) for r in project["dependencies"])
    for edge in EDGES.values():
        assert (SRC / "nodestro_runtime" / edge.probe[0]).is_file()
        assert edge.requirement in extra or edge.requirement == "tabrion"


def _copy_with(tmp_path: Path, file: str, line: str) -> Path:
    target = tmp_path / "src"
    shutil.copytree(SRC / "nodestro_runtime", target / "nodestro_runtime", ignore=shutil.ignore_patterns("__pycache__"))
    path = target / "nodestro_runtime" / file
    text = path.read_text(encoding="utf-8")
    anchor = "from __future__ import annotations\n"
    assert anchor in text, file
    path.write_text(text.replace(anchor, anchor + "\n" + line + "\n", 1), encoding="utf-8")
    return target


@pytest.mark.parametrize("edge", sorted(EDGES))
def test_a_top_level_import_turns_the_edge_red(tmp_path: Path, edge: str) -> None:
    """Mutation probe: a copy with the edge's module imported at the top of a core module fails the check."""
    spec = EDGES[edge]
    result = run_python(check_source(spec), _copy_with(tmp_path, *spec.probe))
    assert result.returncode != 0, f"the probe {spec.probe} went unnoticed"
    assert "ImportError" in result.stderr or "ModuleNotFoundError" in result.stderr
    assert str(tmp_path) in result.stderr  # the copy was imported (the positive control is the edge test on SRC)
