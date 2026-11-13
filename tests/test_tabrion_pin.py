"""VER-1 (TABRION K-34, RC-2/RC-8): every Tabrion requirement of this package is the canonical pin of a final version.

nodestro-runtime is an application in the V-B0 scheme: it names Tabrion by a direct URL, never by a range or a
version specifier, and every Tabrion requirement in ``pyproject.toml`` (dependencies and extras) names the same
commit (one Tabrion version per installation, AC-6). The form is the one nodestro-studio's ``check_versions.py``
demands for every component: ``tabrion[<extras>] @ git+https://github.com/peterkarlhuber/tabrion.git@<40 hex>``.
The pinned commit must be a final release (``0.1.0``, never ``0.1.1.dev0`` or ``0.2.0a1``, RC-8): the installed
Tabrion, which pip or uv took from that commit, says which version it is.
"""

from __future__ import annotations

from importlib import metadata
import json
from pathlib import Path
import re
import tomllib

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version
import pytest

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
CANONICAL = re.compile(r"git\+https://github\.com/peterkarlhuber/tabrion\.git@(?P<sha>[0-9a-f]{40})")


def tabrion_requirements(pyproject: dict) -> list[tuple[str, Requirement]]:
    """``(where, requirement)`` for every Tabrion requirement in the ``[project]`` table."""
    project = pyproject["project"]
    found = [("dependencies", Requirement(line)) for line in project.get("dependencies", [])]
    for extra, lines in project.get("optional-dependencies", {}).items():
        found.extend((f"optional-dependencies.{extra}", Requirement(line)) for line in lines)
    return [(where, req) for where, req in found if canonicalize_name(req.name) == "tabrion"]


def pin_problems(pyproject: dict) -> list[str]:
    """What is wrong with the Tabrion pins of ``pyproject`` (empty: every pin is canonical, all on one commit)."""
    problems: list[str] = []
    shas: set[str] = set()
    for where, req in tabrion_requirements(pyproject):
        if req.url is None:
            problems.append(f"{where}: {req} is not a direct URL pin (an application pins Tabrion, RC-2)")
            continue
        match = CANONICAL.fullmatch(req.url)
        if match is None:
            problems.append(f"{where}: {req.url} is not git+https://github.com/peterkarlhuber/tabrion.git@<40 hex>")
            continue
        if req.specifier:
            problems.append(f"{where}: {req} carries a version specifier next to the pin (RC-8)")
        if req.marker is not None:
            problems.append(f"{where}: {req} carries an environment marker (one Tabrion version everywhere)")
        shas.add(match["sha"])
    if len(shas) > 1:
        problems.append(f"the Tabrion pins name {len(shas)} commits, not one: {', '.join(sorted(shas))}")
    return problems


def pinned_sha() -> str | None:
    shas = {
        CANONICAL.fullmatch(req.url)["sha"]  # type: ignore[index]
        for _where, req in tabrion_requirements(tomllib.loads(PYPROJECT.read_text(encoding="utf-8")))
        if req.url and CANONICAL.fullmatch(req.url)
    }
    return next(iter(shas)) if len(shas) == 1 else None


def test_every_tabrion_requirement_is_the_canonical_pin_of_one_commit() -> None:
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert tabrion_requirements(pyproject), "the dev extra pins tabrion[testing,postgres] (MIG-NOD-1)"
    assert pin_problems(pyproject) == []


@pytest.mark.parametrize(
    ("line", "problem"),
    [
        ("tabrion[testing]>=0.1,<0.2", "not a direct URL pin"),
        ("tabrion==0.1.0", "not a direct URL pin"),
        ("tabrion>=0.1.0a1,<0.2", "not a direct URL pin"),
        ("tabrion @ git+https://github.com/peterkarlhuber/tabrion.git@d5821fa", "is not git+https"),
        ("tabrion @ git+https://github.com/peterkarlhuber/tabrion.git@v0.1.0", "is not git+https"),
        ("tabrion @ git+https://github.com/peterkarlhuber/tabrion@" + "a" * 40, "is not git+https"),
        ("tabrion @ git+ssh://git@github.com/peterkarlhuber/tabrion.git@" + "a" * 40, "is not git+https"),
        ("tabrion @ git+https://github.com/someone/tabrion.git@" + "a" * 40, "is not git+https"),
        ("tabrion @ git+https://github.com/peterkarlhuber/tabrion.git@" + "A" * 40, "is not git+https"),
        (
            "tabrion @ git+https://github.com/peterkarlhuber/tabrion.git@" + "a" * 40 + " ; python_version > '3'",
            "marker",
        ),
    ],
)
def test_a_pin_that_is_not_canonical_is_reported(line: str, problem: str) -> None:
    problems = pin_problems({"project": {"dependencies": [line]}})
    assert len(problems) == 1 and problem in problems[0], problems


def test_two_commits_are_reported() -> None:
    pin = "tabrion[{}] @ git+https://github.com/peterkarlhuber/tabrion.git@{}"
    pyproject = {
        "project": {
            "dependencies": [pin.format("postgres", "a" * 40)],
            "optional-dependencies": {"dev": [pin.format("testing", "b" * 40)]},
        }
    }
    problems = pin_problems(pyproject)
    assert len(problems) == 1 and "2 commits" in problems[0], problems


def test_other_requirements_are_not_checked() -> None:
    assert pin_problems({"project": {"dependencies": ["tabrion-studio>=0.1", "pytest>=8"]}}) == []


def test_the_installed_tabrion_is_the_final_version_of_the_pinned_commit() -> None:
    """The commit is a final release: Tabrion installed from it (``pip install -e ".[dev]"``) has a final version."""
    sha = pinned_sha()
    assert sha is not None
    distribution = metadata.distribution("tabrion")
    version = Version(distribution.version)
    assert not (version.is_prerelease or version.is_devrelease or version.is_postrelease or version.local), (
        f"tabrion {version} is not a final version (RC-8): pin the commit of a final release"
    )
    direct_url = json.loads(distribution.read_text("direct_url.json") or "{}")
    assert direct_url.get("vcs_info", {}).get("commit_id") == sha, (
        f"the installed tabrion {version} is not the pinned commit {sha}: {direct_url}"
    )
