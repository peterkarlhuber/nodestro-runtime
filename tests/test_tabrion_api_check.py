"""AC-7 (TABRION QA-21): only public Tabrion names (``scripts/check_tabrion_api.py``).

The repository passes the check and its exported list ``docs/api-consumers.md`` is current; every
forbidden form is found (mutation probes), and the allowed forms are not.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_tabrion_api.py"
EXPORT = ROOT / "docs" / "api-consumers.md"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_tabrion_api", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load()


def test_the_repository_uses_only_public_tabrion_names_and_the_export_is_current() -> None:
    assert check.main(["--check-export", str(EXPORT)]) == 0


@pytest.mark.parametrize(
    "source",
    [
        "from tabrion._x import y",
        "import tabrion._sa",
        "import tabrion.postgres._pool",
        "import tabrion._sa as sa",
        "from tabrion import _sa",
        "from tabrion.postgres import _configure",
        "from tabrion.postgres._pool import Pool",
        "import tabrion\ntabrion._sa.result",
        "import tabrion\ntabrion.postgres._pool",
        "import tabrion.postgres as pg\npg._pool",
        "from tabrion.postgres import roles\nroles._query(1)",
        "from tabrion.postgres import roles as r\nx = r._query",
        "import tabrion\ngetattr(tabrion, '_sa')",
        "import importlib\nimportlib.import_module('tabrion._sa')",
        "__import__('tabrion._sa')",
        "from tabrion import *",
    ],
)
def test_a_private_name_is_found(source: str) -> None:
    findings, _ = check.scan_source(source)
    assert findings, source


@pytest.mark.parametrize(
    ("source", "used"),
    [
        ("from tabrion import errors", {"tabrion.errors"}),
        ("import tabrion\ntabrion.__version__", {"tabrion", "tabrion.__version__"}),
        (
            "import tabrion.postgres\ntabrion.postgres.roles.check_role(c)",
            {"tabrion.postgres", "tabrion.postgres.roles.check_role"},
        ),
        (
            "from tabrion.postgres import roles as r\nr.check_role(c)",
            {"tabrion.postgres.roles", "tabrion.postgres.roles.check_role"},
        ),
        ("import os\nos._exit", set()),
        ("from .tabrion import _x", set()),
        ("from tabrion_studio import _private", set()),
    ],
)
def test_public_names_are_allowed_and_listed(source: str, used: set[str]) -> None:
    findings, names = check.scan_source(source)
    assert findings == [] and names == used


def test_the_surface_comparison_refuses_names_outside_the_snapshot(tmp_path: Path) -> None:
    surface = tmp_path / "surface.json"
    names: dict[str, dict[str, str]] = {
        "tabrion.errors.safe_message": {},
        "tabrion.postgres.roles.check_role": {},
    }
    surface.write_text(json.dumps({"names": names}), encoding="utf-8")
    ok = {"tabrion.errors", "tabrion.errors.safe_message", "tabrion.postgres.roles", "tabrion"}
    assert check.surface_check(ok, surface) == []
    assert check.surface_check({"tabrion.errors.classify_more"}, surface) != []


def test_the_export_lists_each_name_with_its_files() -> None:
    text = check.render({"tabrion.errors.safe_message": {"src/b.py", "src/a.py"}})
    assert "| `tabrion.errors.safe_message` | `src/a.py`, `src/b.py` |" in text
    assert "no Tabrion name" in check.render({})
