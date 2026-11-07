"""Write the SQL parity baseline ``tests/fixtures/sql_parity_before.json`` (MUP-ONA B4, step 2; test T-PAR-1).

MUP-ONA B4 moves the SQL of the PostgreSQL index, file store and memory (``postgres_tables.py``) into a statement
catalog. The tenant SQL check sees a statement in the source today; after the move it sees the rendered catalog. The
baseline is the list of every statement the check finds in ``postgres_tables.py`` before any code change, so the
parity test can prove that nothing drops out of the check: every statement of the baseline must be found again, in the
source or in the rendered catalog, compared normalized.

Each entry holds:

* ``module``: the module as the check names it (``nodestro_runtime/postgres_tables.py``);
* ``text``: the statement as the check rebuilds it (whitespace collapsed, unresolved parts as ``{source}``);
* ``canonical``: the statement as PostgreSQL's grammar prints it back (``pglast`` deparse of the text with psycopg's
  placeholders as ``$n``), so a text that differs only in quoting, case of keywords or layout compares equal;
  ``null`` when the text is dynamic or does not parse;
* ``findings``: the kinds of finding the check reports for it (``dynamic``, ``unparsed``, ``leak``, ``unsupported``),
  sorted, without duplicates; empty when the statement is proven bound to one tenant;
* ``allowlisted``: whether an entry of ``tenant_sql_allowlist.py`` covers it (then it is not checked).

Entries are sorted by ``(module, text)`` and line numbers are left out, so a change of layout elsewhere in the module
does not change the file. Usage, from the repository root with the dev dependencies installed::

    python tests/fixtures/make_sql_parity.py              # writes tests/fixtures/sql_parity_before.json
    python tests/fixtures/make_sql_parity.py --out -      # prints it

The baseline is written once, at the commit before B4 changes code, and never regenerated after that commit: it is the
"before" side of the comparison.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

import nodestro_runtime
from nodestro_runtime import tenant_sql_allowlist
from nodestro_runtime.tenant_sql_check import check_sql, statements_of, tenant_tables, to_postgres_params

#: The modules whose statements the baseline lists (relative to the package's parent, as the check names them).
MODULES = ("nodestro_runtime/postgres_tables.py",)
DEFAULT_OUT = Path(__file__).parent / "sql_parity_before.json"


def canonical(text: str) -> str | None:
    """``text`` as PostgreSQL's grammar prints it back, or ``None`` when it does not parse."""
    from pglast import parse_sql
    from pglast.parser import ParseError
    from pglast.stream import RawStream

    try:
        return "; ".join(RawStream()(raw) for raw in parse_sql(to_postgres_params(text)))
    except ParseError:
        return None


def baseline() -> dict[str, Any]:
    """The baseline of :data:`MODULES` as the check sees them now."""
    from pglast.parser import ParseError

    package = Path(nodestro_runtime.__file__).parent
    tables = tenant_tables(package / "migrations" / "postgres")
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for module in MODULES:
        source = (package.parent / module).read_text(encoding="utf-8")
        for statement in statements_of(source, module, tables):
            key = (module, statement.text)
            if key in seen:  # the same text at two places is one statement of the catalog
                continue
            seen.add(key)
            kinds: set[str] = set()
            allowlisted = key in tenant_sql_allowlist.ALLOWLIST
            if statement.dynamic:
                kinds.add("dynamic")
            else:
                try:
                    problems = check_sql(statement.text, tables)
                except ParseError:
                    kinds.add("unparsed")
                else:
                    kinds.update("unsupported" if "not supported" in p else "leak" for p in problems)
            entries.append(
                {
                    "module": module,
                    "text": statement.text,
                    "canonical": None if statement.dynamic else canonical(statement.text),
                    "findings": [] if allowlisted else sorted(kinds),
                    "allowlisted": allowlisted,
                }
            )
    entries.sort(key=lambda e: (e["module"], e["text"]))
    return {
        "description": "Statements the tenant SQL check finds before MUP-ONA B4 (tests/fixtures/make_sql_parity.py).",
        "modules": list(MODULES),
        "tenant_tables": sorted(tables),
        "statements": entries,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output file, or - for standard output")
    args = parser.parse_args(argv)
    text = json.dumps(baseline(), indent=2, ensure_ascii=False) + "\n"
    if args.out == "-":
        sys.stdout.write(text)
    else:
        Path(args.out).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
