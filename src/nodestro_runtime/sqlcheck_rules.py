"""Rule engine of the tenant SQL check: is one PostgreSQL statement bound to one tenant? (MUP-ONA B4)

:func:`check_statement` parses a statement with PostgreSQL's own grammar (``pglast``) and reports every way it may read
or change rows of another tenant. It knows nothing of where the statement comes from, of Nodestro's tables or of an
allowlist: the tables that are partitioned by tenant, the name of the partition column and the placeholder style are
parameters. It imports only ``pglast`` (late, so importing this module needs no parser) and the standard library, so
it can be taken over unchanged as ``orkexa.testing.sqlcheck`` (MUP-ONA B5); an import-linter contract in
``pyproject.toml`` keeps it so. Nodestro's source code check (:mod:`nodestro_runtime.tenant_sql_check`) rebuilds the
statements from Python source and calls this engine for each.

The rules, with ``tenant_id`` standing for ``column`` and a *tenant table* for any name in ``tenant_tables`` (in any
case):

- every reference to a tenant table -- in ``FROM``, joins, ``USING``, the target of ``UPDATE``/``DELETE``,
  subqueries, CTEs and set operations -- is bound by a top-level ``AND`` conjunct of its statement's ``WHERE`` (or of
  an inner join's ``ON``; a left join's ``ON`` binds its right side, a right join's its left side) of the form
  ``<ref>.tenant_id = <param>`` (a type cast around either side is allowed), or by ``<ref>.tenant_id =
  <other>.tenant_id`` with ``<other>`` bound (in the same statement or an enclosing one; a name of the statement's own
  ``FROM`` hides an outer one). An unqualified ``tenant_id`` binds only when the statement has a single tenant table
  reference. A CTE named like a tenant table is a leak (it would hide the table);
- ``<ref>.tenant_id = ANY(<param>)`` may hold several tenants: it binds only with ``allow_any=True``. Without it the
  reference is a ``leak``, with a second finding that says why;
- ``INSERT`` into a tenant table must list ``tenant_id`` with a parameter (or the ``tenant_id`` of a reference of its
  own ``SELECT``) as the value; an upsert that updates must have ``tenant_id`` in its conflict target; ``UPDATE``
  (and an upsert, except ``tenant_id = excluded.tenant_id``) must not set ``tenant_id``;
- a string constant that names a tenant table and is more than a (qualified) name (``DO $$ ... $$``,
  ``query_to_xml('...')``), and every statement other than ``SELECT``/``INSERT``/``UPDATE``/``DELETE``/``EXPLAIN``
  and the schema statements (``CREATE``, ``ALTER``, ``CREATE INDEX``, ``COMMENT``, ``GRANT``, ``DROP``, ``VACUUM``,
  ``LOCK``, policies) that names a tenant table (``TRUNCATE``, ``COPY``, ``MERGE`` ...) is ``unsupported``;
- a text the parser rejects is ``unparsed``.

A statement passes only when the list is empty; ``unparsed`` and ``unsupported`` are failures too, never a pass.

Blind spots (what the engine cannot see): whether the bound parameter holds the right tenant (the caller passes it);
SQL inside a string or function body (refused as ``unsupported`` when it names a tenant table, not checked); views,
functions, triggers and rules on the server that reach a tenant table under another name; a tenant table reached by
a name not in ``tenant_tables`` (a schema-qualified name counts by its last part; a ``search_path`` that resolves
an unlisted name to a tenant table does not); and a placeholder bound to an expression that is not one tenant.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
import importlib
import re
from typing import Any, Literal

__all__ = ["ANY_REFUSED", "KINDS", "PLACEHOLDERS", "Finding", "check_statement", "to_postgres_params"]

#: The kinds of finding: a reference that may reach another tenant's rows, a text the parser rejects, a statement the
#: rules cannot decide.
KINDS = frozenset({"leak", "unparsed", "unsupported"})
#: Placeholder styles: psycopg's (``%s``, ``%(name)s``, ``%b``, ``%t``; ``%%`` is ``%``) or PostgreSQL's own (``$1``).
PLACEHOLDERS = frozenset({"psycopg", "postgres"})

#: Statements that change the schema, not rows: allowed on tenant tables (migrations, indexes, grants).
_SCHEMA_STATEMENTS = frozenset(
    {
        "CreateStmt",
        "IndexStmt",
        "AlterTableStmt",
        "CommentStmt",
        "GrantStmt",
        "DropStmt",
        "VacuumStmt",
        "LockStmt",
        "CreatePolicyStmt",
        "AlterPolicyStmt",
    }
)
_DATA_STATEMENTS = frozenset({"SelectStmt", "InsertStmt", "UpdateStmt", "DeleteStmt"})
#: A (possibly qualified) name: a string that is only a name reads no rows (``relname = 'documents'``).
_IDENTIFIER = re.compile(r"[\w$]+(?:\.[\w$]+)*")
#: A column name as the parser gives an unquoted identifier.
_COLUMN = re.compile(r"[a-z_][a-z0-9_$]*")
#: Why a reference named only by a tenant id array parameter is reported (``allow_any`` off).
ANY_REFUSED = "an array of tenant ids may hold several tenants"


@dataclass(frozen=True)
class Finding:
    """One reason why a statement may read or change another tenant's rows, or cannot be decided.

    ``position`` is the offset (in characters, from 0) in the text as given of the table reference or statement the
    finding is about, as far as the parser tells it (best effort: a hint for the reader, not part of the rule)."""

    kind: Literal["leak", "unparsed", "unsupported"]
    position: int
    message: str

    def __str__(self) -> str:
        return f"[{self.kind}] at {self.position}: {self.message}"


def _pglast(module: str) -> Any:
    """pglast (or one of its modules), untyped: its node attributes are all optional in its stubs."""
    return importlib.import_module(f"pglast.{module}" if module else "pglast")


def _to_postgres(text: str) -> tuple[str, list[int]]:
    """psycopg's placeholders as ``$n``, with the offset in ``text`` of every character of the result (and of its
    end)."""
    out: list[str] = []
    origin: list[int] = []
    names: dict[str, int] = {}
    count = 0
    index = 0

    def emit(value: str, start: int) -> None:
        out.append(value)
        origin.extend([start] * len(value))

    while index < len(text):
        char = text[index]
        if char != "%":
            emit(char, index)
            index += 1
            continue
        following = text[index + 1 : index + 2]
        if following == "%":
            emit("%", index)
            index += 2
        elif following in ("s", "b", "t"):
            count += 1
            emit(f"${count}", index)
            index += 2
        elif following == "(":
            end = text.find(")", index)
            if end == -1 or text[end + 1 : end + 2] not in ("s", "b", "t"):
                emit(char, index)
                index += 1
                continue
            name = text[index + 2 : end]
            if name not in names:
                count += 1
                names[name] = count
            emit(f"${names[name]}", index)
            index = end + 2
        else:
            emit(char, index)
            index += 1
    origin.append(len(text))
    return "".join(out), origin


def to_postgres_params(text: str) -> str:
    """psycopg's placeholders (``%s``, ``%(name)s``, ``%b``, ``%t``) as ``$n``; ``%%`` as ``%``."""
    return _to_postgres(text)[0]


@dataclass
class _Ref:
    name: str  # alias or table name, as columns qualify it
    table: str
    location: int
    bound: bool = False
    #: Only a refused ``tenant_id = ANY(<param>)`` names it (``allow_any`` off): reported with that reason.
    any_refused: bool = False


def _location(node: Any, default: int) -> int:
    value = getattr(node, "location", None)
    return value if isinstance(value, int) and value >= 0 else default


def _children(node: Any) -> Iterator[Any]:
    pg = _pglast("ast")

    if isinstance(node, (list, tuple)):
        for item in node:
            yield from _children(item) if isinstance(item, (list, tuple)) else iter([item])
        return
    if isinstance(node, pg.Node):
        for slot in node.__slots__:
            value = getattr(node, slot, None)
            if isinstance(value, pg.Node):
                yield value
            elif isinstance(value, (list, tuple)):
                yield from _children(value)


def _walk(node: Any) -> Iterator[Any]:
    yield node
    for child in _children(node):
        yield from _walk(child)


def _conjuncts(expr: Any) -> list[Any]:
    pg = _pglast("ast")
    BoolExprType = _pglast("enums").BoolExprType

    if expr is None:
        return []
    if isinstance(expr, pg.BoolExpr) and expr.boolop == BoolExprType.AND_EXPR:
        return [c for arg in expr.args for c in _conjuncts(arg)]
    return [expr]


def _is_param(expr: Any) -> bool:
    pg = _pglast("ast")

    while isinstance(expr, pg.TypeCast):
        expr = expr.arg
    return isinstance(expr, pg.ParamRef)


def _words(text: str) -> set[str]:
    word: list[str] = []
    found: set[str] = set()
    for char in text.lower() + " ":
        if char.isalnum() or char == "_":
            word.append(char)
        elif word:
            found.add("".join(word))
            word = []
    return found


class _Checker:
    def __init__(self, tables: frozenset[str], column: str, *, allow_any: bool, start: int) -> None:
        self.tables = tables
        #: The partition column (``tenant_id``).
        self.column = column
        #: Whether ``tenant_id = ANY(<param>)`` binds a reference (off by default: the array may hold several tenants).
        self.allow_any = allow_any
        #: Where the statement starts (for problems without a location of their own).
        self.start = start
        self.problems: list[tuple[int, str]] = []

    def _problem(self, location: int, message: str) -> None:
        self.problems.append((location, message))

    def _partition_column(self, expr: Any) -> tuple[str, ...] | None:
        """The qualifier of a partition column reference (``()`` when unqualified), else ``None``."""
        pg = _pglast("ast")

        while isinstance(expr, pg.TypeCast):
            expr = expr.arg
        if not isinstance(expr, pg.ColumnRef):
            return None
        names = tuple(getattr(f, "sval", None) for f in expr.fields)
        if not names or names[-1] != self.column or any(n is None for n in names):
            return None
        return tuple(str(n) for n in names[:-1])

    def _from_refs(self, items: Any, ctes: set[str]) -> tuple[list[_Ref], list[tuple[Any, list[_Ref]]], set[str]]:
        """Tenant table references of a FROM list, the join conditions that bind them (``quals``, refs) and every name
        the FROM list defines (tenant table or not: an inner name hides an outer one)."""
        pg = _pglast("ast")
        JoinType = _pglast("enums").JoinType

        refs: list[_Ref] = []
        quals: list[tuple[Any, list[_Ref]]] = []
        names: set[str] = set()

        def visit(item: Any) -> list[_Ref]:
            alias = getattr(item, "alias", None)
            if alias is not None and getattr(alias, "aliasname", None):
                names.add(alias.aliasname)
            if isinstance(item, pg.RangeVar):
                if alias is None:
                    names.add(item.relname)  # an alias hides the table's own name
                table = item.relname.lower()
                if table in self.tables and not (item.schemaname is None and item.relname in ctes):
                    ref = _Ref(item.alias.aliasname if item.alias else item.relname, table, _location(item, self.start))
                    refs.append(ref)
                    return [ref]
                return []
            if isinstance(item, pg.JoinExpr):
                left, right = visit(item.larg), visit(item.rarg)
                if item.quals is not None:
                    if item.jointype == JoinType.JOIN_INNER:
                        quals.append((item.quals, left + right))
                    elif item.jointype == JoinType.JOIN_LEFT:
                        quals.append((item.quals, right))
                    elif item.jointype == JoinType.JOIN_RIGHT:
                        quals.append((item.quals, left))
                return left + right
            return []  # subselects and functions are statements of their own

        for item in items or ():
            visit(item)
        return refs, quals, names

    def _bind(
        self,
        refs: list[_Ref],
        pools: list[tuple[Any, list[_Ref]]],
        outer: list[tuple[set[str], list[_Ref]]],
        names: set[str],
    ) -> None:
        """Mark refs bound by ``tenant_id = param`` (``= ANY(param)`` only with ``allow_any``) or by equality with a
        bound ref. A qualifier means the nearest name: this statement's own (which hides an outer one of the same
        name), then the enclosing statements' from the inside out."""
        pg = _pglast("ast")
        A_Expr_Kind = _pglast("enums").A_Expr_Kind

        def partner_binds(qualifier: str, ref: _Ref) -> bool:
            if qualifier in names:
                own = [r for r in refs if r.name == qualifier]
                return len(own) == 1 and own[0] is not ref and own[0].bound
            for scope_names, scope_refs in reversed(outer):
                if qualifier in scope_names:
                    # An enclosing statement's ref: reported there if it is not bound itself.
                    return len([r for r in scope_refs if r.name == qualifier]) == 1
            return False

        def lookup(qualifier: tuple[str, ...], candidates: list[_Ref]) -> _Ref | None:
            if not qualifier:  # unqualified: only unambiguous with a single tenant table in the statement
                return refs[0] if len(refs) == 1 and any(r is refs[0] for r in candidates) else None
            matches = [r for r in candidates if r.name == qualifier[-1]]
            return matches[0] if len(matches) == 1 else None

        changed = True
        while changed:
            changed = False
            for expr, applies in pools:
                for conjunct in _conjuncts(expr):
                    if not isinstance(conjunct, pg.A_Expr) or conjunct.lexpr is None or conjunct.rexpr is None:
                        continue
                    if [getattr(n, "sval", None) for n in conjunct.name] != ["="]:
                        continue
                    if conjunct.kind not in (A_Expr_Kind.AEXPR_OP, A_Expr_Kind.AEXPR_OP_ANY):
                        continue
                    sides = [(conjunct.lexpr, conjunct.rexpr), (conjunct.rexpr, conjunct.lexpr)]
                    if conjunct.kind == A_Expr_Kind.AEXPR_OP_ANY:
                        sides = sides[:1]  # tenant_id = ANY(param)
                        if not self.allow_any:
                            qualifier = self._partition_column(conjunct.lexpr)
                            ref = None if qualifier is None else lookup(qualifier, applies)
                            if ref is not None and _is_param(conjunct.rexpr):
                                ref.any_refused = True
                            continue
                    for column, other in sides:
                        qualifier = self._partition_column(column)
                        if qualifier is None:
                            continue
                        ref = lookup(qualifier, applies)
                        if ref is None or ref.bound:
                            continue
                        if _is_param(other):
                            ref.bound = changed = True
                            continue
                        other_qualifier = self._partition_column(other)
                        if conjunct.kind != A_Expr_Kind.AEXPR_OP or other_qualifier is None or not other_qualifier:
                            continue
                        if partner_binds(other_qualifier[-1], ref):
                            ref.bound = changed = True

    def _check_insert(self, node: Any, table: str) -> None:
        """``tenant_id`` is listed and its value is a parameter (or a bound ``tenant_id``); an upsert's conflict target
        includes ``tenant_id``, so the update it turns into cannot hit another tenant's row."""
        pg = _pglast("ast")
        OnConflictAction = _pglast("enums").OnConflictAction

        column = self.column
        location = _location(node.relation, self.start)
        columns = [c.name for c in node.cols or ()]
        if column not in columns:
            self._problem(location, f"the INSERT into {table} lists no {column} column")
            return
        index = columns.index(column)
        select = node.selectStmt
        if isinstance(select, pg.SelectStmt) and select.valuesLists:
            values = [row[index] if index < len(row) else None for row in select.valuesLists]
        elif isinstance(select, pg.SelectStmt) and select.targetList and select.larg is None:
            own, _quals, _names = self._from_refs(select.fromClause, set())
            target = select.targetList[index].val if index < len(select.targetList) else None
            qualifier = self._partition_column(target)
            from_own = qualifier is not None and (
                [r for r in own if r.name == qualifier[-1]] if qualifier else own[:1] if len(own) == 1 else []
            )
            values = [None if not from_own and not _is_param(target) else target]
        else:
            values = [None]
        if not all(
            value is not None and (_is_param(value) or self._partition_column(value) is not None) for value in values
        ):
            self._problem(location, f"the {column} written into {table} is not a parameter or a bound {column}")
        clause = node.onConflictClause
        if clause is not None and clause.action == OnConflictAction.ONCONFLICT_UPDATE:
            infer = clause.infer
            target_columns = [e.name for e in (infer.indexElems or ())] if infer is not None else []
            if column not in target_columns:
                self._problem(
                    _location(clause, location),
                    f"the conflict target of an upsert into {table} lacks {column}: it may update another tenant's row",
                )
            for item in clause.targetList or ():
                if item.name == column and not (
                    column in target_columns and self._partition_column(item.val) == ("excluded",)
                ):
                    self._problem(
                        _location(item, location),
                        f"the upsert into {table} sets {column}: it moves a row to another tenant",
                    )

    def check(self, node: Any, outer: list[tuple[set[str], list[_Ref]]], ctes: set[str]) -> None:
        pg = _pglast("ast")

        own: list[_Ref] = []
        names: set[str] = set()
        if isinstance(node, (pg.SelectStmt, pg.UpdateStmt, pg.DeleteStmt, pg.InsertStmt)):
            with_clause = getattr(node, "withClause", None)
            if with_clause is not None:
                for cte in with_clause.ctes or ():
                    if cte.ctename in self.tables:
                        self._problem(_location(cte, self.start), f"CTE named like the tenant table {cte.ctename}")
                    ctes = ctes | {cte.ctename}
        if isinstance(node, pg.SelectStmt):
            own, quals, names = self._from_refs(node.fromClause, ctes)
            self._bind(own, [(node.whereClause, own), *quals], outer, names)
        elif isinstance(node, (pg.UpdateStmt, pg.DeleteStmt)):
            target = [node.relation]
            extra = node.fromClause if isinstance(node, pg.UpdateStmt) else node.usingClause
            own, quals, names = self._from_refs([*target, *(extra or ())], ctes)
            self._bind(own, [(node.whereClause, own), *quals], outer, names)
            if isinstance(node, pg.UpdateStmt) and node.relation.relname.lower() in self.tables:
                for item in node.targetList or ():
                    if item.name == self.column:
                        self._problem(
                            _location(item, self.start),
                            f"the UPDATE of {node.relation.relname} sets {self.column}: it moves a row to another "
                            "tenant",
                        )
                        break
        elif isinstance(node, pg.InsertStmt):
            table = node.relation.relname
            if table.lower() in self.tables:
                self._check_insert(node, table)
        for ref in own:
            if not ref.bound:
                self._problem(
                    ref.location,
                    f"{ref.table}"
                    + (f" (as {ref.name})" if ref.name != ref.table else "")
                    + f" is not bound to one tenant by a top-level `{self.column} = <param>` conjunct",
                )
                if ref.any_refused and all(message != ANY_REFUSED for _, message in self.problems):
                    self._problem(ref.location, ANY_REFUSED)
        for child in _children(node):
            self.check(child, [*outer, (names, own)], ctes)


def check_statement(
    sql: str,
    *,
    tenant_tables: Iterable[str],
    column: str = "tenant_id",
    placeholder: str = "psycopg",
    allow_any: bool = False,
) -> list[Finding]:
    """Why ``sql`` (one or more PostgreSQL statements) may read or change rows of another tenant, or cannot be decided;
    empty when every reference to a table of ``tenant_tables`` is bound to one tenant by ``column`` (see the module's
    rules). ``placeholder``: ``"psycopg"`` (``%s``, ``%(name)s``) or ``"postgres"`` (``$1``). ``allow_any``: ``column =
    ANY(<param>)`` binds too (off by default: the array may hold several tenants). Never raises for a text the parser
    rejects: that is an ``unparsed`` finding."""
    parser = _pglast("parser")

    if placeholder not in PLACEHOLDERS:
        raise ValueError(f"placeholder must be one of {sorted(PLACEHOLDERS)}, not {placeholder!r}")
    if not _COLUMN.fullmatch(column):
        raise ValueError(f"column must be an unquoted lower-case PostgreSQL name, not {column!r}")
    tables = frozenset(name.lower() for name in tenant_tables)
    if placeholder == "psycopg":
        text, origin = _to_postgres(sql)
    else:
        text, origin = sql, list(range(len(sql) + 1))

    def position(location: int) -> int:
        return origin[min(max(location, 0), len(origin) - 1)]

    try:
        parsed = parser.parse_sql(text)
    except parser.ParseError as exc:
        location = exc.args[1] if len(exc.args) > 1 and isinstance(exc.args[1], int) else 0
        return [Finding("unparsed", position(location), str(exc))]
    findings: list[Finding] = []
    for raw in parsed:
        stmt = raw.stmt
        start = raw.stmt_location or 0
        kind = type(stmt).__name__
        # SQL in a string (a DO block, a function body, query_to_xml('...')) is run by the server, not checked here.
        for node in _walk(stmt):
            value = getattr(node, "sval", None) if type(node).__name__ == "String" else None
            if isinstance(value, str) and _words(value) & tables and not _IDENTIFIER.fullmatch(value):
                findings.append(
                    Finding(
                        "unsupported",
                        position(start),
                        f"a string that names a tenant table ({value[:80]!r}) is not supported by the check",
                    )
                )
        if kind in _DATA_STATEMENTS or kind == "ExplainStmt":
            checker = _Checker(tables, column, allow_any=allow_any, start=start)
            checker.check(stmt, [], set())
            findings.extend(Finding("leak", position(at), message) for at, message in checker.problems)
            continue
        touched = sorted(
            {n.relname for n in _walk(stmt) if type(n).__name__ == "RangeVar" and n.relname.lower() in tables}
        )
        if not touched or kind in _SCHEMA_STATEMENTS:
            continue
        findings.append(
            Finding("unsupported", position(start), f"{kind} on {', '.join(touched)} is not supported by the check")
        )
    return findings
