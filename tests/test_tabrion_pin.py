"""VER-1 (TABRION K-34, RC-1/RC-8): every Tabrion requirement of this package names one minor range of final versions.

Since the CI-FAST pin switch, nodestro-runtime names its internal packages by version ranges, and the exact commits
live in one lock (nodestro-studio ``platform/internal-server.txt``). For Tabrion, every requirement in
``pyproject.toml`` (dependencies and extras) names the same range ``>=0.N,<0.(N+1)``: no direct URL, no other
specifier, no pre-release bound and no environment marker (one Tabrion version per installation, AC-6). The installed
Tabrion, which the lock chose, must be a final release inside that range (``0.1.0``, never ``0.1.1.dev0`` or
``0.2.0a1``, RC-8).
"""

from __future__ import annotations

from importlib import metadata
from pathlib import Path
import tomllib

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version
import pytest

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def tabrion_requirements(pyproject: dict) -> list[tuple[str, Requirement]]:
    """``(where, requirement)`` for every Tabrion requirement in the ``[project]`` table."""
    project = pyproject["project"]
    found = [("dependencies", Requirement(line)) for line in project.get("dependencies", [])]
    for extra, lines in project.get("optional-dependencies", {}).items():
        found.extend((f"optional-dependencies.{extra}", Requirement(line)) for line in lines)
    return [(where, req) for where, req in found if canonicalize_name(req.name) == "tabrion"]


def minor_range(specifier: SpecifierSet) -> tuple[int, int] | None:
    """``(0, N)`` when ``specifier`` is exactly ``>=0.N,<0.(N+1)`` (final bounds), else ``None``."""
    specs = {spec.operator: spec.version for spec in specifier}
    if len(specs) != 2 or set(specs) != {">=", "<"}:
        return None
    low, high = Version(specs[">="]), Version(specs["<"])
    for bound in (low, high):
        if bound.is_prerelease or bound.is_devrelease or bound.is_postrelease or bound.local:
            return None
    if any(low.release[2:]) or any(high.release[2:]):
        return None
    if low.major != 0 or high.major != 0 or high.minor != low.minor + 1:
        return None
    return (low.major, low.minor)


def range_problems(pyproject: dict) -> list[str]:
    """What is wrong with the Tabrion requirements of ``pyproject`` (empty: one minor range everywhere)."""
    problems: list[str] = []
    ranges: set[tuple[int, int]] = set()
    for where, req in tabrion_requirements(pyproject):
        if req.url is not None:
            problems.append(f"{where}: {req} is a direct URL; the commit belongs in the studio lock (RC-1)")
            continue
        if req.marker is not None:
            problems.append(f"{where}: {req} carries an environment marker (one Tabrion version everywhere)")
            continue
        found = minor_range(req.specifier)
        if found is None:
            problems.append(f"{where}: {req} does not name the range >=0.N,<0.(N+1) of final versions (RC-8)")
            continue
        ranges.add(found)
    if len(ranges) > 1:
        names = ", ".join(f">={major}.{minor},<{major}.{minor + 1}" for major, minor in sorted(ranges))
        problems.append(f"the Tabrion requirements name {len(ranges)} ranges, not one: {names}")
    return problems


def test_every_tabrion_requirement_names_one_minor_range() -> None:
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert tabrion_requirements(pyproject), "the dev extra requires tabrion[testing,postgres] (MIG-NOD-1)"
    assert range_problems(pyproject) == []


@pytest.mark.parametrize(
    ("line", "problem"),
    [
        ("tabrion @ git+https://github.com/peterkarlhuber/tabrion.git@" + "a" * 40, "direct URL"),
        ("tabrion==0.1.0", "does not name the range"),
        ("tabrion>=0.1", "does not name the range"),
        ("tabrion>=0.1,<0.3", "does not name the range"),
        ("tabrion>=0.1,<1.0", "does not name the range"),
        ("tabrion>=0.1.0a1,<0.2", "does not name the range"),
        ("tabrion>=0.1.dev0,<0.2", "does not name the range"),
        ("tabrion>=0.1.1,<0.2", "does not name the range"),
        ("tabrion>=0.1,<0.2,!=0.1.3", "does not name the range"),
        ("tabrion>=0.1,<0.2 ; python_version > '3'", "marker"),
    ],
)
def test_a_requirement_that_is_not_one_minor_range_is_reported(line: str, problem: str) -> None:
    problems = range_problems({"project": {"dependencies": [line]}})
    assert len(problems) == 1 and problem in problems[0], problems


def test_two_ranges_are_reported() -> None:
    pyproject = {
        "project": {
            "dependencies": ["tabrion[postgres]>=0.1,<0.2"],
            "optional-dependencies": {"dev": ["tabrion[testing]>=0.2,<0.3"]},
        }
    }
    problems = range_problems(pyproject)
    assert len(problems) == 1 and "2 ranges" in problems[0], problems


def test_the_same_range_in_two_places_is_fine() -> None:
    pyproject = {
        "project": {
            "dependencies": ["tabrion[postgres]>=0.1,<0.2"],
            "optional-dependencies": {"dev": ["tabrion[testing,postgres]>=0.1.0,<0.2.0"]},
        }
    }
    assert range_problems(pyproject) == []


def test_other_requirements_are_not_checked() -> None:
    assert range_problems({"project": {"dependencies": ["tabrion-studio>=0.1", "pytest>=8"]}}) == []


def test_the_installed_tabrion_is_a_final_version_inside_the_range() -> None:
    """The lock chose a final release: Tabrion installed from it (``pip install -c <lock> -e ".[dev]"``)."""
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert range_problems(pyproject) == []
    specifier = tabrion_requirements(pyproject)[0][1].specifier
    version = Version(metadata.version("tabrion"))
    assert not (version.is_prerelease or version.is_devrelease or version.is_postrelease or version.local), (
        f"tabrion {version} is not a final version (RC-8): the lock must name the commit of a final release"
    )
    assert version in specifier, f"tabrion {version} is outside {specifier}"
