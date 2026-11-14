"""VER-1 (TABRION K-34, RC-1/RC-8): every Tabrion requirement of this package names one minor range of final versions.

Since the CI-FAST pin switch, nodestro-runtime names its internal packages by version ranges, and the exact commits
live in one lock (nodestro-studio ``platform/internal-server.txt``). For Tabrion, every requirement in
``pyproject.toml`` (dependencies and extras) names the same range ``>=0.N,<0.(N+1)``: no direct URL, no other
specifier, no pre-release bound and no environment marker (one Tabrion version per installation, AC-6). The installed
Tabrion, which the lock chose, must be a final release inside that range (``0.2.1``, never ``0.2.2.dev0`` or
``0.3.0a1``, RC-8).

DC-NR-1 (dependency confusion): the installed Tabrion's ``direct_url.json`` must name a git commit; when
``NODESTRO_STUDIO_LOCK`` names the lock file (CI sets it to the studio lock it installs with), the URL and commit
must be the lock's ``tabrion @ git+URL@SHA``. This detects an accidental install from a package index (a version
check alone passed one); it is no proof of origin, because a wheel can ship a forged ``direct_url.json``. The
protection is the direct-URL pin of the install (``-c <lock>``), and the proof that it never asks an index for
Tabrion is CI's ``scripts/check_tabrion_source.py`` job ``tabrion source (poisoned index)`` (zero requests).
"""

from __future__ import annotations

from importlib import metadata
import json
import os
from pathlib import Path
import re
import tomllib
from typing import Any

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version
import pytest

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
LOCK_ENV = "NODESTRO_STUDIO_LOCK"
#: The lock's Tabrion line: ``tabrion[extras] @ git+URL@<40 hex>``.
#: The minor line every Tabrion requirement names today (``>=0.2,<0.3``, Tabrion v0.2.1 final in the studio lock).
CURRENT_RANGE = (0, 2)
LOCK_LINE = re.compile(r"^\s*tabrion\s*(?:\[[^\]]*\])?\s*@\s*git\+(?P<url>\S+?)@(?P<commit>[0-9a-f]{40})\b")


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
    found = {minor_range(req.specifier) for _, req in tabrion_requirements(pyproject)}
    assert found == {CURRENT_RANGE}, f"expected >=0.{CURRENT_RANGE[1]},<0.{CURRENT_RANGE[1] + 1}, found {found}"


@pytest.mark.parametrize(
    ("line", "problem"),
    [
        ("tabrion @ git+https://github.com/peterkarlhuber/tabrion.git@" + "a" * 40, "direct URL"),
        ("tabrion==0.2.1", "does not name the range"),
        ("tabrion>=0.2", "does not name the range"),
        ("tabrion>=0.1,<0.3", "does not name the range"),
        ("tabrion>=0.2,<0.4", "does not name the range"),
        ("tabrion>=0.2,<1.0", "does not name the range"),
        ("tabrion>=0.2.0a1,<0.3", "does not name the range"),
        ("tabrion>=0.2.dev0,<0.3", "does not name the range"),
        ("tabrion>=0.2,<0.3.dev0", "does not name the range"),
        ("tabrion>=0.2.1,<0.3", "does not name the range"),
        ("tabrion>=0.2,<0.3,!=0.2.0", "does not name the range"),
        ("tabrion>=0.2,<0.3 ; python_version > '3'", "marker"),
    ],
)
def test_a_requirement_that_is_not_one_minor_range_is_reported(line: str, problem: str) -> None:
    problems = range_problems({"project": {"dependencies": [line]}})
    assert len(problems) == 1 and problem in problems[0], problems


def test_two_ranges_are_reported() -> None:
    pyproject = {
        "project": {
            "dependencies": ["tabrion[postgres]>=0.2,<0.3"],
            "optional-dependencies": {"dev": ["tabrion[testing]>=0.1,<0.2"]},
        }
    }
    problems = range_problems(pyproject)
    assert len(problems) == 1 and "2 ranges" in problems[0], problems


def test_the_same_range_in_two_places_is_fine() -> None:
    pyproject = {
        "project": {
            "dependencies": ["tabrion[postgres]>=0.2,<0.3"],
            "optional-dependencies": {"dev": ["tabrion[testing,postgres]>=0.2.0,<0.3.0"]},
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


def locked_tabrion(text: str) -> tuple[str, str] | None:
    """``(url, commit)`` of the lock's Tabrion line, or ``None``."""
    for line in text.splitlines():
        found = LOCK_LINE.match(line.split("#", 1)[0])
        if found:
            return found["url"], found["commit"]
    return None


def _strip(url: str) -> str:
    return url.removeprefix("git+").rstrip("/").removesuffix(".git")


def origin_problems(direct_url: dict[str, Any] | None, lock: tuple[str, str] | None) -> list[str]:
    """What is wrong with where Tabrion came from (``direct_url.json``), against the lock's ``(url, commit)``."""
    if not direct_url:
        return ["tabrion has no direct_url.json: installed from a package index, not from the lock's commit"]
    vcs = direct_url.get("vcs_info") or {}
    url = str(direct_url.get("url", ""))
    commit = str(vcs.get("commit_id", ""))
    if vcs.get("vcs") != "git" or not re.fullmatch(r"[0-9a-f]{40}", commit):
        return [f"tabrion is not installed from a git commit ({url})"]
    if lock is None:
        return []
    if _strip(url) != _strip(lock[0]):
        return [f"tabrion is installed from {url}, the lock pins {lock[0]}"]
    if commit != lock[1]:
        return [f"tabrion is installed at {commit[:12]}, the lock pins {lock[1][:12]}"]
    return []


SHA = "af0458d54467571abfc1afdd44d76e1f4d727c06"
URL = "https://github.com/peterkarlhuber/tabrion.git"


@pytest.mark.parametrize(
    ("direct_url", "problem"),
    [
        (None, "installed from a package index"),
        ({"url": "file:///src/tabrion", "dir_info": {"editable": True}}, "not installed from a git commit"),
        ({"url": URL, "vcs_info": {"vcs": "git", "commit_id": "e" * 40}}, "installed at eeeeeeeeeeee, the lock pins"),
        ({"url": "https://evil.invalid/tabrion.git", "vcs_info": {"vcs": "git", "commit_id": SHA}}, "the lock pins"),
    ],
)
def test_a_tabrion_not_from_the_locked_commit_is_reported(direct_url: dict[str, Any] | None, problem: str) -> None:
    problems = origin_problems(direct_url, (URL, SHA))
    assert len(problems) == 1 and problem in problems[0], problems


def test_the_locked_commit_passes_in_any_url_spelling() -> None:
    lock = locked_tabrion(f"nodestro-api @ git+https://x/y.git@{'a' * 40}\ntabrion[testing] @ git+{URL}@{SHA}\n")
    assert lock == (URL, SHA)
    for url in (URL, URL.removesuffix(".git"), URL + "/"):
        assert origin_problems({"url": url, "vcs_info": {"vcs": "git", "commit_id": SHA}}, lock) == []
    assert locked_tabrion(f"# tabrion @ git+{URL}@{SHA}\ntabrion>=0.2,<0.3\n") is None


def test_the_installed_tabrion_is_the_locked_commit() -> None:
    """DC-NR-1: the installed Tabrion's direct_url.json names the lock's git commit (detects an index install)."""
    text = metadata.distribution("tabrion").read_text("direct_url.json")
    direct_url = json.loads(text) if text else None
    lock_file = os.environ.get(LOCK_ENV)
    lock = None
    if lock_file:
        lock = locked_tabrion(Path(lock_file).read_text(encoding="utf-8"))
        assert lock is not None, f"{lock_file} ({LOCK_ENV}) has no line tabrion @ git+URL@<40 hex>"
    elif direct_url and direct_url.get("dir_info") is not None:
        pytest.skip(f"tabrion is installed from a local directory and {LOCK_ENV} is unset: no commit to compare")
    assert origin_problems(direct_url, lock) == []
