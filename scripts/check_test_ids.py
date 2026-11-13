"""CI-FAST AP6: every collected test ran exactly once.

Usage: python scripts/check_test_ids.py COLLECTED.txt JUNIT.xml [JUNIT.xml ...]

COLLECTED.txt is the output of ``pytest --collect-only -q`` with the same selection (paths, -m) as the test run;
the JUnit files are the reports of that run (one per shard, or one from a pytest-xdist run). Each collected test ID
must appear exactly once in the reports, and the reports must hold no test that was not collected. Any difference
(a lost, duplicated or extra test) fails with the IDs, so a split of the suite can never drop a test silently.

A module skipped while it is collected (``pytest.importorskip`` or ``pytest.skip(allow_module_level=True)`` at module
level, e.g. without psycopg) has no test IDs in COLLECTED.txt; pytest reports it as one entry with an empty classname,
the module as its name and ``<skipped message="collection skipped">``. Such an entry is not a test: it is listed and
left out of the comparison, but only if no collected ID belongs to that module.
"""

from __future__ import annotations

import collections
import re
import sys
import xml.etree.ElementTree as ET

# A collected test ID ends in a test name or a parameter list; the summary line ("N tests collected") does not.
_ID = re.compile(r"^\S+::\S.*$")


def junit_name(node_id: str) -> tuple[str, str]:
    """The (classname, name) pytest's JUnit report gives the test with this node ID (_pytest.junitxml)."""
    path, bracket, params = node_id.partition("[")
    names = path.split("::")
    names[0] = re.sub(r"\.py$", "", names[0].replace("/", "."))
    names[-1] += bracket + params
    return ".".join(names[:-1]), names[-1]


def junit_module(node_id: str) -> str:
    """The dotted module of a node ID, as the JUnit report names a module skipped at collection."""
    return re.sub(r"\.py$", "", node_id.split("::", 1)[0].replace("/", "."))


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    with open(argv[0], encoding="utf-8") as handle:
        collected_ids = [line.strip() for line in handle if _ID.match(line.strip())]
    if not collected_ids:
        print(f"::error::No test IDs in {argv[0]}")
        return 1
    collected = collections.Counter(junit_name(node_id) for node_id in collected_ids)
    reported: collections.Counter[tuple[str, str]] = collections.Counter()
    skipped_modules: collections.Counter[str] = collections.Counter()
    collected_modules = {junit_module(node_id) for node_id in collected_ids}
    for path in argv[1:]:
        for case in ET.parse(path).getroot().iter("testcase"):
            key = (case.get("classname", ""), case.get("name", ""))
            skipped = case.find("skipped")
            if (
                key[0] == ""
                and skipped is not None
                and skipped.get("message") == "collection skipped"
                and key[1] not in collected_modules
            ):
                skipped_modules[key[1]] += 1
                continue
            reported[key] += 1
    problems = []
    for key, count in sorted(collected.items()):
        if count > 1:
            problems.append(f"collected {count} times: {key[0]}::{key[1]}")
    for key in sorted(collected.keys() - reported.keys()):
        problems.append(f"collected but not run: {key[0]}::{key[1]}")
    for key in sorted(reported.keys() - collected.keys()):
        problems.append(f"run but not collected: {key[0]}::{key[1]}")
    for key, count in sorted(reported.items()):
        if count > 1:
            problems.append(f"run {count} times: {key[0]}::{key[1]}")
    for module, count in sorted(skipped_modules.items()):
        print(f"module skipped at collection ({count} report entr{'y' if count == 1 else 'ies'}): {module}")
    total = sum(reported.values())
    if problems:
        for problem in problems[:200]:
            print(f"::error::{problem}")
        print(f"{len(problems)} differences; collected {len(collected_ids)}, reported {total}")
        return 1
    print(f"OK: {len(collected_ids)} collected test IDs, each reported exactly once ({len(argv) - 1} report(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
