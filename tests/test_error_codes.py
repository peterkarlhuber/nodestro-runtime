"""RT-011 (X-03): the catalog of problem codes matches the source, so a new code never reaches the UI unnoticed."""

from __future__ import annotations

from pathlib import Path
import textwrap

from nodestro_runtime import error_codes
from nodestro_runtime.error_codes import CODE, catalog, collect, scan


def test_the_catalog_lists_exactly_the_codes_of_the_source() -> None:
    shipped, current = catalog(), collect()
    missing = sorted(set(current) - set(shipped))
    stale = sorted(set(shipped) - set(current))
    changed = sorted(code for code in set(current) & set(shipped) if current[code] != shipped[code])
    hint = "run: python -m nodestro_runtime.error_codes --write (and translate new codes in the studio)"
    assert not missing, f"codes missing from error_codes.json: {missing}; {hint}"
    assert not stale, f"codes no longer in the source: {stale}; {hint}"
    assert not changed, f"codes whose parameters changed: {changed}; {hint}"
    assert error_codes.main(["--check"]) == 0


def test_every_entry_is_a_well_formed_code_with_sorted_parameters() -> None:
    for code, params in catalog().items():
        assert CODE.fullmatch(code), code
        assert params == sorted(set(params)), code
    # Codes the studio relies on are in it, with their parameters.
    assert catalog()["run.nodes_failed"] == ["nodes"]
    assert "run.budget_exceeded" in catalog()
    assert {"type", "rounds", "looping"} <= set(catalog()["run.agent.MaxToolRoundsError"])
    assert "local.unreachable" in catalog() and "resume.was_failed" in catalog()


def test_the_scan_finds_codes_in_calls_and_tables(tmp_path: Path) -> None:
    (tmp_path / "module.py").write_text(
        textwrap.dedent(
            """
            from x import NodestroError, problem

            REASONS = {"rejected": "demo.rejected", "file": "demo.json"}

            def check(value):
                if value:
                    raise NodestroError("demo.too_large", "Too large", limit=3, message_kind="x")
                return problem("demo.empty", "Empty", level="warning", field="name")

            def prefix(kind):
                return f"demo.was_{kind}", "demo.was_"
            """
        )
    )
    found = scan(tmp_path, exclude={"demo.json"})
    assert found == {
        "demo.too_large": {"limit", "message_kind"},
        "demo.empty": set(),  # level and field describe the issue, they are not parameters
        "demo.rejected": set(),
    }
