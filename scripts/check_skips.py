"""Compare the skipped tests of a CI job with its expectation file (MUP-ONA A0, T-SKIP-1).

Usage: python scripts/check_skips.py <junit.xml> tests/ci/skipped-<job>.txt

The JUnit XML comes from ``pytest --junitxml``. Every test case with a ``<skipped>`` element counts as skipped,
except expected failures (``type="pytest.xfail"``): pytest writes those as ``<skipped>`` too, but an xfail is a
failing test kept visible, not a skip. A skipped test is named ``<classname>::<name>`` as the JUnit file gives it,
e.g. ``tests.test_check_skips::test_an_entry_without_a_reason_is_an_error``; the parametrization stays part of
the name.

The expectation file lists one skipped test per line as ``<name>  # <reason>``. Blank lines and lines that start
with ``#`` are ignored. An entry without a reason is an error (exit code 2). Any difference between the skipped set
and the expected set prints both lists and exits with code 1. Only the standard library is used.
"""

from __future__ import annotations

from pathlib import Path
import sys
import xml.etree.ElementTree as ET


def skipped_tests(junit: Path) -> set[str]:
    """The names of the skipped (not xfailed) test cases in a JUnit XML file."""
    names: set[str] = set()
    for case in ET.parse(junit).getroot().iter("testcase"):
        skipped = case.find("skipped")
        if skipped is None or skipped.get("type") == "pytest.xfail":
            continue
        names.add(f"{case.get('classname', '')}::{case.get('name', '')}")
    return names


def expected_skips(path: Path) -> tuple[set[str], list[str]]:
    """The expected names, and the lines that have no reason."""
    names: set[str] = set()
    errors: list[str] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, reason = line.partition("#")
        if not separator or not reason.strip() or not name.strip():
            errors.append(f"{path}:{number}: entry needs '<test>  # <reason>': {line}")
            continue
        names.add(name.strip())
    return names, errors


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_skips.py <junit.xml> <expectation file>", file=sys.stderr)
        return 2
    junit, expectation = Path(argv[0]), Path(argv[1])
    expected, errors = expected_skips(expectation)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 2
    actual = skipped_tests(junit)
    unexpected = sorted(actual - expected)
    missing = sorted(expected - actual)
    if not unexpected and not missing:
        print(f"{len(actual)} skipped tests, as expected in {expectation}")
        return 0
    print(f"The skipped tests differ from {expectation}.")
    print(f"Skipped but not expected ({len(unexpected)}):")
    for name in unexpected:
        print(f"  {name}")
    print(f"Expected but not skipped ({len(missing)}):")
    for name in missing:
        print(f"  {name}")
    print("Fix the cause, or update the expectation file with a reason (and justify the change in the PR).")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
