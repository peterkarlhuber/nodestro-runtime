"""The CI skip check (``scripts/check_skips.py``): the skipped tests of a job must match its expectation file."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
from types import ModuleType

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_skips.py"
EXPECTATIONS = Path(__file__).resolve().parent / "ci"

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest">
  <testcase classname="tests.test_a" name="test_runs"/>
  <testcase classname="tests.test_a" name="test_skipped[x]"><skipped type="pytest.skip" message="no x"/></testcase>
  <testcase classname="tests.test_b.TestC" name="test_d"><skipped type="pytest.skip" message="no d"/></testcase>
  <testcase classname="tests.test_b" name="test_xfail"><skipped type="pytest.xfail" message="known"/></testcase>
</testsuite></testsuites>
"""


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_skips", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(tmp_path: Path, expectation: str) -> subprocess.CompletedProcess[str]:
    junit = tmp_path / "junit.xml"
    junit.write_text(JUNIT, encoding="utf-8")
    expected = tmp_path / "skipped.txt"
    expected.write_text(expectation, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(junit), str(expected)], capture_output=True, text=True, check=False
    )


# MUP-ONA T-SKIP-1
def test_matching_skips_pass_and_xfails_are_not_skips(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "# comment\n\ntests.test_a::test_skipped[x]  # no x here\ntests.test_b.TestC::test_d  # needs d\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 skipped tests" in result.stdout


# MUP-ONA T-SKIP-1
def test_an_unexpected_or_missing_skip_fails(tmp_path: Path) -> None:
    result = _run(tmp_path, "tests.test_a::test_skipped[x]  # no x\ntests.test_a::test_runs  # was skipped once\n")
    assert result.returncode == 1
    assert "tests.test_b.TestC::test_d" in result.stdout  # skipped but not expected
    assert "tests.test_a::test_runs" in result.stdout  # expected but not skipped


# MUP-ONA T-SKIP-1
def test_an_entry_without_a_reason_is_an_error(tmp_path: Path) -> None:
    assert _run(tmp_path, "tests.test_a::test_skipped[x]\ntests.test_b.TestC::test_d  # d\n").returncode == 2
    assert _run(tmp_path, "tests.test_a::test_skipped[x]  #   \ntests.test_b.TestC::test_d  # d\n").returncode == 2


# MUP-ONA T-SKIP-1
def test_every_expectation_file_has_reasons() -> None:
    module = _module()
    files = sorted(EXPECTATIONS.glob("skipped-*.txt"))
    assert files, "tests/ci/skipped-<job>.txt is missing"
    for path in files:
        _names, errors = module.expected_skips(path)
        assert errors == [], errors
