"""TABRION AC-2 (QA-17): the manifest follows this repository's copy of the dependency policy K-33.

``scripts/check_dependency_policy.py`` and ``dependency-policy.json`` are unchanged copies from
nodestro-studio; ``scripts/DEPENDENCY_POLICY_SOURCE.md`` records their origin and SHA-256. The tests
check the copies, run the checker on ``pyproject.toml`` and add one forbidden requirement per probe,
which must turn red.
"""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import re
import subprocess
import sys
import tomllib
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_dependency_policy.py"
POLICY = ROOT / "dependency-policy.json"
SOURCE = ROOT / "scripts" / "DEPENDENCY_POLICY_SOURCE.md"
COMPONENT = "nodestro-runtime"


def _checker() -> Any:
    spec = importlib.util.spec_from_file_location("check_dependency_policy", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up by name
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("path", [SCRIPT, POLICY], ids=["script", "policy"])
def test_the_copies_are_unchanged(path: Path) -> None:
    recorded = re.search(rf"`{re.escape(path.name)}`: `([0-9a-f]{{64}})`", SOURCE.read_text(encoding="utf-8"))
    assert recorded is not None, f"{path.name} is not recorded in {SOURCE.name}"
    # A Windows checkout may turn LF into CRLF (core.autocrlf); the recorded SHA-256 is of the LF file.
    content = path.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(content).hexdigest() == recorded[1]


def test_the_manifest_follows_the_policy() -> None:
    result = subprocess.run([sys.executable, str(SCRIPT)], cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert f"; {COMPONENT})" in result.stdout


def test_the_copy_holds_only_this_component() -> None:
    policy = _checker().load_policy(POLICY)
    assert policy.errors == []
    assert policy.component == COMPONENT
    assert {edge.source for edge in policy.edges} <= {COMPONENT}


@pytest.mark.parametrize(
    ("requirements", "extra", "message"),
    [
        pytest.param(
            '["nodestro-api>=0.1"]', None, "no edge nodestro-runtime -> nodestro-api", id="platform-to-consumer"
        ),
        pytest.param(
            '["tabrion[postgres]>=0.2,<0.3"]', None, "are not in the policy's []", id="core-edge-without-extras"
        ),
        pytest.param('["tabrion[postgres]>=0.2,<0.3"]', "pdf", "not allowed here", id="wrong-extra"),
        pytest.param('["nodestro"]', "ui", "no edge nodestro-runtime -> nodestro-studio", id="studio"),
        pytest.param('["nodestro-unknown>=0.1"]', None, "unknown Levuvo package", id="unknown-package"),
    ],
)
def test_a_forbidden_requirement_turns_red(requirements: str, extra: str | None, message: str) -> None:
    checker = _checker()
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    probe = tomllib.loads(f"items = {requirements}")["items"]
    project = pyproject["project"]
    if extra is None:
        project["dependencies"] = [*project.get("dependencies", []), *probe]
    else:
        extras = project.setdefault("optional-dependencies", {})
        extras[extra] = [*extras.get(extra, []), *probe]
    errors = checker.load_policy(POLICY).check_manifest(COMPONENT, pyproject)
    assert len(errors) == 1, errors
    assert message in errors[0]
