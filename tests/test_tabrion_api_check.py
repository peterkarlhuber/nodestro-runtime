"""AC-7 (TABRION QA-21): only public Tabrion names (``scripts/check_tabrion_api.py``).

The repository passes the check and its exported list ``docs/api-consumers.md`` is current; every
forbidden form is found (mutation probes), and the allowed forms are not. A stale or missing export fails.
The ``__all__`` check runs against a fake ``tabrion`` package, so it is exercised before any repository
installs Tabrion; experimental (``X``) names of the K10 snapshot are refused. The script is the same file in
tabrion-studio, axentra, orkexa and nodestro-runtime: its SHA-256 is pinned here, so a change to one copy
fails until all four tests are updated with the new copy.
"""

from __future__ import annotations

from collections.abc import Iterator
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_tabrion_api.py"
EXPORT = ROOT / "docs" / "api-consumers.md"
#: SHA-256 of ``scripts/check_tabrion_api.py``, the same in all four repositories.
SCRIPT_SHA256 = "bc16c3141efb96e1249498ee42f054b75f630c50e0df2c161882ed7f5dd528d8"


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


def test_the_script_is_the_shared_copy() -> None:
    assert hashlib.sha256(SCRIPT.read_bytes()).hexdigest() == SCRIPT_SHA256


@pytest.mark.parametrize("content", ["# Tabrion API used by this repository\n\nstale\n", None])
def test_a_stale_or_missing_export_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str | None
) -> None:
    export = tmp_path / "api-consumers.md"
    if content is not None:
        export.write_text(content, encoding="utf-8")
    assert check.main(["--check-export", str(export)]) == 1
    assert f"{export} is stale: run --export {export}" in capsys.readouterr().out


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
        "import importlib\nimportlib.import_module(name='tabrion._sa')",
        "import importlib\nimportlib.import_module('._sa', 'tabrion')",
        "from importlib import import_module\nimport_module('._sa', package='tabrion')",
        "from importlib import import_module as im\nim('tabrion._sa')",
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
        ("import importlib\nimportlib.import_module('tabrion.errors')", {"tabrion.errors"}),
        ("import importlib\nimportlib.import_module('.errors', 'tabrion')", {"tabrion.errors"}),
        ("import importlib\nimportlib.import_module('._x', 'tabrion_studio')", set()),
        ("import os\nos._exit", set()),
        ("from .tabrion import _x", set()),
        ("from tabrion_studio import _private", set()),
    ],
)
def test_public_names_are_allowed_and_listed(source: str, used: set[str]) -> None:
    findings, names = check.scan_source(source)
    assert findings == [] and names == used


def test_the_surface_comparison_refuses_names_outside_the_snapshot_and_experimental_names(
    tmp_path: Path,
) -> None:
    surface = tmp_path / "surface.json"
    names: dict[str, dict[str, str]] = {
        "tabrion.Database": {"stability": "S"},
        "tabrion.Database.run_transaction": {"stability": "X"},
        "tabrion.errors.safe_message": {"stability": "S"},
        "tabrion.errors.old": {},
        "tabrion.postgres.roles.check_role": {"stability": "S"},
    }
    surface.write_text(json.dumps({"names": names}), encoding="utf-8")
    ok = {
        "tabrion.errors",
        "tabrion.errors.safe_message",
        "tabrion.postgres.roles",
        "tabrion",
        "tabrion.Database",
    }
    assert check.surface_check(ok, surface) == []
    assert check.surface_check({"tabrion.errors.classify_more"}, surface) == [
        "tabrion.errors.classify_more: not in surface.json"
    ]
    assert check.surface_check({"tabrion.Database.run_transaction", "tabrion.errors.old"}, surface) == [
        "tabrion.Database.run_transaction: stability X in surface.json, consumers use only S names",
        "tabrion.errors.old: stability None in surface.json, consumers use only S names",
    ]


# ---- the __all__ check against a fake installed Tabrion ------------------------------------------------------

_FAKE = {
    "__init__.py": '__all__ = ["Database"]\n__version__ = "0"\nDatabase = object\nhidden = 1\n',
    "errors.py": '__all__ = ["safe_message"]\nsafe_message = str\nre = None\n',
    "plain.py": "value = 1\n",
    "migrations.py": "import alembic_is_not_installed_here  # noqa: F401\n__all__ = ['SchemaStatus']\n",
}


def _forget_tabrion() -> dict[str, ModuleType]:
    gone = {name: module for name, module in sys.modules.items() if name == "tabrion" or name.startswith("tabrion.")}
    for name in gone:
        del sys.modules[name]
    return gone


@pytest.fixture
def fake_tabrion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    package = tmp_path / "site" / "tabrion"
    package.mkdir(parents=True)
    for name, text in _FAKE.items():
        (package / name).write_text(text, encoding="utf-8")
    saved = _forget_tabrion()
    monkeypatch.syspath_prepend(str(package.parent))
    importlib.invalidate_caches()
    yield package
    _forget_tabrion()
    sys.modules.update(saved)


def test_names_in_all_and_modules_pass(fake_tabrion: Path) -> None:
    assert check.tabrion_installed()
    used = {
        "tabrion",
        "tabrion.Database",
        "tabrion.__version__",
        "tabrion.errors",
        "tabrion.errors.safe_message",
    }
    assert check.installed_check(used) == []


def test_names_outside_all_are_refused(fake_tabrion: Path) -> None:
    assert check.installed_check({"tabrion.hidden", "tabrion.errors.re", "tabrion.plain.value"}) == [
        "tabrion.errors.re: re is not in tabrion.errors.__all__",
        "tabrion.hidden: hidden is not in tabrion.__all__",
        "tabrion.plain.value: value is not in tabrion.plain.__all__",
    ]


def test_a_module_that_needs_a_missing_extra_names_the_import_error(fake_tabrion: Path) -> None:
    (problem,) = check.installed_check({"tabrion.migrations.SchemaStatus"})
    assert problem.startswith("tabrion.migrations.SchemaStatus: cannot import tabrion.migrations (")
    assert "alembic_is_not_installed_here" in problem and "__all__" not in problem


def test_main_runs_the_all_check(fake_tabrion: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = tmp_path / "uses.py"
    source.write_text("import tabrion\ntabrion.hidden\n", encoding="utf-8")
    assert check.main([str(source)]) == 1
    assert "tabrion.hidden: hidden is not in tabrion.__all__" in capsys.readouterr().out


def test_used_names_without_an_installed_tabrion_fail_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(check, "tabrion_installed", lambda: False)
    assert check.installed_check(set()) == []
    (problem,) = check.installed_check({"tabrion.Database"})
    assert "tabrion is not installed" in problem


def test_a_directory_named_tabrion_is_not_an_installed_tabrion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if importlib.util.find_spec("tabrion") is not None:
        pytest.skip("a real Tabrion is installed; a namespace directory cannot shadow it")
    (tmp_path / "tabrion").mkdir()
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    assert importlib.util.find_spec("tabrion") is not None  # found, as a namespace package
    assert not check.tabrion_installed()


def test_the_export_lists_each_name_with_its_files() -> None:
    text = check.render({"tabrion.errors.safe_message": {"src/b.py", "src/a.py"}})
    assert "| `tabrion.errors.safe_message` | `src/a.py`, `src/b.py` |" in text
    assert "no Tabrion name" in check.render({})
