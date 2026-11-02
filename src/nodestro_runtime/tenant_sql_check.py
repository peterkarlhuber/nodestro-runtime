"""Static check that every PostgreSQL statement on a tenant table is bound to one tenant (security review SR-01).

Nodestro's own tables (``nodestro_*``) have no row-level security: a store is bound to one tenant and every statement
filters on (or writes) ``tenant_id`` (``docs/AXENTRA.md``, "Row-level security on Nodestro's tables"). This module is
the control that keeps it so. It is used by the test suites of nodestro-runtime and nodestro-api; nothing imports it at
run time, and its parser (``pglast``, PostgreSQL's own grammar) is a test dependency only.

How it works:

- :func:`tenant_tables` reads the migrations and returns every ``nodestro_*`` table with a ``tenant_id`` column, so a
  new table is covered as soon as its migration exists.
- :func:`scan_package` reads every ``.py`` file of a package with :mod:`ast` and rebuilds the text of each string
  expression the way Python would build it: string and bytes constants, implicit and ``+`` concatenation (adjacent
  constants without a gap, so ``"nodestro_" + "documents"`` is the table's name), f-strings, ``str.format``,
  ``str % values`` (``%s`` replaced by the text of its value from a literal tuple, a literal dict or a single value),
  ``str.join`` (of a literal list, or of a name holding one), an item of a literal tuple, list or dict that nothing
  changes (``QS[0]``), and psycopg's ``sql.SQL(...).format(...)`` / ``sql.Identifier`` / ``sql.Placeholder`` /
  ``sql.Composed``; a string method that changes the text (``.replace``, ``.lower`` ...) adds an unknown part.
- A name is resolved to every value it can hold where it is read -- inside a string expression and also on its own,
  as in ``q += " OR ..."; db._query(q)`` or ``q = q + ...``: module constants (and assignments of functions that
  declare it ``global``), the assignments and ``+=`` of the same function before the read (and after it, in a loop
  around it), each conditional assignment or ``+=`` taken and not taken, ``for`` loops over a tuple of strings. A
  parameter starts as an unknown part, and its ``+=`` follow it. A closure or lambda reads a name of the enclosing
  function with every value it can hold there (any assignment may be the last one, any ``+=`` may have happened); a
  ``nonlocal`` assignment of a nested function counts like a ``global`` one. A ``+=`` that a loop (or a nested
  function) repeats, or ``q = q + ...`` in a loop, is an unknown part. Every combination is one variant, and every
  variant is checked; more than :data:`MAX_VARIANTS` is reported as ``dynamic``.
- ``+=`` on a target that is not followed as a name (``self.q``, ``Q["a"]``, a name in a class body) is checked as an
  unknown part followed by its value.
- Each variant whose known parts name a tenant table, that starts like a data statement and contains a part that
  cannot be resolved, or that starts with a part that cannot be resolved (an imported fragment, ``self.BASE``, a
  function's result, a parameter) followed by SQL, is a candidate. SQL means a keyword in capitals, a comment
  (``--`` before a space or the end, ``/*``), or a keyword in lower case in a SQL shape (``or true``, ``and id =
  %s``, ``union select``, ``limit %s``, ``; delete``). A text that is not part of a known statement and continues one
  (``" OR true"`` as a class attribute, a tuple item or a call argument: it starts with ``OR``, ``AND``, ``WHERE``,
  ``UNION``, ``LIMIT``, ``ORDER BY`` ... and holds SQL) is a candidate too. A candidate with an unresolved part is
  reported as ``dynamic``; one that the PostgreSQL parser rejects is reported as ``unparsed``. Neither passes
  silently: such a text passes only when it is fixed or through an allowlist entry with its reason.
- A parsed candidate passes when every reference to a tenant table (its name in any case) -- in ``FROM``, joins,
  ``USING``, the target of ``UPDATE``/``DELETE``, subqueries, CTEs and set operations -- is bound by a top-level
  ``AND`` conjunct of its statement's ``WHERE`` (or of an inner join's ``ON``) of the form ``<ref>.tenant_id =
  <param>`` or ``<ref>.tenant_id = ANY(<param>)``, or by ``<ref>.tenant_id = <other>.tenant_id`` with ``<other>``
  bound (in the same statement or an enclosing one; a name of the statement's own ``FROM`` hides an outer one).
  ``INSERT`` into a tenant table must list ``tenant_id`` with a parameter (or a bound ``tenant_id`` of its
  ``SELECT``) as the value; an upsert that updates must have ``tenant_id`` in its conflict target; ``UPDATE`` (and
  an upsert, except ``tenant_id = excluded.tenant_id``) must not set ``tenant_id``. A string literal or code body
  that names a tenant table (``DO $$ ... $$``, ``query_to_xml('...')``), ``TRUNCATE``, ``COPY``, ``MERGE`` and other
  statements that touch a tenant table are reported as ``unsupported``.
- Everything that is cross-tenant (or not a PostgreSQL statement) by design is listed in an allowlist that maps the
  module and the **whole** normalized text (whitespace collapsed) to its reason. Matching is exact; an entry that no
  longer matches anything is reported as stale.

What it guarantees: every text in the scanned packages that is, or may become, a PostgreSQL statement on a tenant
table, as far as its parts are written in the package's source, is either proven bound to one tenant by the rules
above, reported, or allowlisted with a reason. What it does not guarantee (``docs/AXENTRA.md``, "Remaining isolation
risk"): that the parameter is the right tenant (the store bound to one tenant passes it); SQL from outside the
scanned packages or read at run time (a file, a setting, the database); and SQL that only run-time data puts
together without a known part that shows it -- an argument of a helper that runs whatever it is given (checked at the
callers that pass a literal text), a text made only of unknown parts whose fragments live in another package, a
lower-case keyword without a SQL shape after an unknown part (``{BASE} or deleted``), ``getattr``/``eval``.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator, Mapping
import dataclasses
from dataclasses import dataclass, field
import itertools
from pathlib import Path
import re
import string
from typing import Any

#: At most this many variants of one expression are checked; more is reported as ``dynamic``.
MAX_VARIANTS = 256
#: Statement keywords that read or change rows (a candidate with an unresolved part that starts with one is reported).
_DATA_KEYWORDS = frozenset({"select", "insert", "update", "delete", "with", "merge", "copy", "truncate", "table"})
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


@dataclass(frozen=True)
class Hole:
    """A part of a string expression whose value is not known statically."""

    source: str


@dataclass(frozen=True)
class Statement:
    """One variant of a string expression that is checked as SQL."""

    module: str
    line: int
    text: str  # whitespace collapsed; unresolved parts as ``{source}``
    dynamic: bool


@dataclass(frozen=True)
class Finding:
    module: str
    line: int
    kind: str  # leak | dynamic | unparsed | unsupported | stale
    text: str
    detail: str

    def __str__(self) -> str:
        return f"{self.module}:{self.line}: [{self.kind}] {self.detail}: {self.text[:300]}"


@dataclass
class Report:
    checked: list[Statement] = field(default_factory=list)
    allowed: list[Statement] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


def _pglast(module: str) -> Any:
    """pglast (or one of its modules), untyped: its node attributes are all optional in its stubs."""
    import importlib

    return importlib.import_module(f"pglast.{module}" if module else "pglast")


# --- tenant tables -----------------------------------------------------------------------------------------------


def tenant_tables(migrations: Path) -> frozenset[str]:
    """Every ``nodestro_*`` table with a ``tenant_id`` column, from the migration files (``*.sql``) in ``migrations``.

    Columns added later (``ALTER TABLE ... ADD COLUMN tenant_id``) count. A migration the parser rejects raises."""
    pg = _pglast("ast")
    parse_sql = _pglast("").parse_sql

    found: set[str] = set()
    files = sorted(migrations.glob("*.sql"))
    if not files:
        raise FileNotFoundError(f"no migrations in {migrations}")
    for path in files:
        for raw in parse_sql(path.read_text(encoding="utf-8")):
            stmt = raw.stmt
            if isinstance(stmt, pg.CreateStmt):
                columns = [c.colname for c in stmt.tableElts or () if isinstance(c, pg.ColumnDef)]
                if "tenant_id" in columns:
                    found.add(stmt.relation.relname)
            elif isinstance(stmt, pg.AlterTableStmt):
                for cmd in stmt.cmds or ():
                    definition = getattr(cmd, "def_", None)
                    if isinstance(definition, pg.ColumnDef) and definition.colname == "tenant_id":
                        found.add(stmt.relation.relname)
    return frozenset(name for name in found if name.startswith("nodestro_"))


# --- Python side: rebuild the text of string expressions --------------------------------------------------------


def _product(parts: list[list[list[Any]]]) -> list[list[Any]] | None:
    total = 1
    for options in parts:
        total *= max(len(options), 1)
        if total > MAX_VARIANTS:
            return None
    return [list(itertools.chain.from_iterable(combo)) for combo in itertools.product(*parts)]


def _is_sql_attr(node: ast.expr, name: str) -> bool:
    """``sql.<name>``, ``psycopg.sql.<name>`` or a bare ``<name>`` imported from psycopg.sql."""
    if isinstance(node, ast.Attribute) and node.attr == name:
        return True
    return isinstance(node, ast.Name) and node.id == name


Position = tuple[int, int]

#: String methods whose result is the text of the receiver (for the strip methods: without arguments).
_SAME_TEXT = frozenset({"encode", "decode", "strip", "lstrip", "rstrip"})
#: String methods that change the text of the receiver: the result is the receiver followed by an unknown part.
_CHANGED_TEXT = frozenset(
    {
        "replace",
        "translate",
        "format_map",
        "removeprefix",
        "removesuffix",
        "lower",
        "upper",
        "casefold",
        "swapcase",
        "title",
        "capitalize",
        "expandtabs",
        "zfill",
        "center",
        "ljust",
        "rjust",
    }
)
_LOOPS = (ast.For, ast.AsyncFor, ast.While)
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _start(node: ast.AST) -> Position:
    return (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))


def _end(node: ast.AST) -> Position:
    end_line = getattr(node, "end_lineno", None)
    end_col = getattr(node, "end_col_offset", None)
    if end_line is None or end_col is None:
        return _start(node)
    return (end_line, end_col)


def _inside(position: Position, node: ast.AST) -> bool:
    return _start(node) <= position <= _end(node)


@dataclass(frozen=True, eq=False)
class _Event:
    """An assignment to a name in one scope."""

    position: Position  # where the new value takes effect: the end of the statement (of the iterable for ``for``)
    kind: str  # set | add (``+=``) | for (a loop target) | unknown
    value: ast.AST
    conditional: bool  # it may not happen before the use (a branch, a loop body, a handler)
    loops: tuple[ast.AST, ...]  # the loops of the scope around it (for a ``global``: the function)
    node: ast.AST


def _body(owner: ast.AST) -> list[ast.stmt]:
    body = getattr(owner, "body", [])
    return body if isinstance(body, list) else []


def _walruses(node: ast.AST) -> Iterator[ast.NamedExpr]:
    """``name := ...`` in the expressions of statement ``node`` (not in nested statements or functions)."""
    for _field, value in ast.iter_fields(node):
        for item in value if isinstance(value, list) else [value]:
            if not isinstance(item, ast.expr):
                continue
            stack: list[ast.AST] = [item]
            while stack:
                current = stack.pop()
                if isinstance(current, ast.Lambda):
                    continue
                if isinstance(current, ast.NamedExpr):
                    yield current
                stack.extend(ast.iter_child_nodes(current))


def _stored(target: ast.AST) -> Iterator[str]:
    for node in ast.walk(target):
        if isinstance(node, ast.Name):
            yield node.id


def _collect_events(owner: ast.AST, module: bool) -> dict[str, list[_Event]]:
    """The assignments of every name directly in ``owner`` (not in nested functions or classes), in source order.
    For the module, the assignments of functions that declare a name ``global`` count too (as conditional, at any
    time)."""
    events: dict[str, list[_Event]] = {}
    only: set[str] | None = None

    def add(
        name: str,
        node: ast.AST,
        position: Position,
        kind: str,
        value: ast.AST,
        conditional: bool,
        loops: tuple[ast.AST, ...],
    ) -> None:
        if only is None or name in only:
            events.setdefault(name, []).append(_Event(position, kind, value, conditional, loops, node))

    def visit(nodes: Iterable[ast.AST], conditional: bool, loops: tuple[ast.AST, ...]) -> None:
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            for walrus in _walruses(node):
                add(walrus.target.id, walrus, _end(walrus), "unknown", walrus.value, conditional, loops)
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        add(target.id, node, _end(node), "set", node.value, conditional, loops)
                    elif isinstance(target, (ast.Tuple, ast.List, ast.Starred)):
                        for name in _stored(target):
                            add(name, node, _end(node), "unknown", node.value, conditional, loops)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.value is not None:
                    add(node.target.id, node, _end(node), "set", node.value, conditional, loops)
            elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
                kind = "add" if isinstance(node.op, ast.Add) else "unknown"
                add(node.target.id, node, _end(node), kind, node.value, conditional, loops)
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                if isinstance(node.target, ast.Name):
                    add(node.target.id, node, _end(node.iter), "for", node.iter, conditional, loops)
                else:
                    for name in _stored(node.target):
                        add(name, node, _end(node.iter), "unknown", node.iter, conditional, loops)
                visit(node.body, True, (*loops, node))
                visit(node.orelse, True, loops)
            elif isinstance(node, ast.While):
                visit(node.body, True, (*loops, node))
                visit(node.orelse, True, loops)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is not None:
                        for name in _stored(item.optional_vars):
                            add(name, node, _end(item.context_expr), "unknown", item.context_expr, conditional, loops)
                visit(node.body, conditional, loops)
            elif isinstance(node, (ast.If, ast.Try, ast.Match)) or type(node).__name__ == "TryStar":
                for child_field in ("body", "orelse", "finalbody", "handlers", "cases"):
                    for child in getattr(node, child_field, None) or []:
                        if isinstance(child, (ast.ExceptHandler, ast.match_case)):
                            if isinstance(child, ast.ExceptHandler) and child.name:
                                add(child.name, child, _start(child), "unknown", child, True, loops)
                            if isinstance(child, ast.match_case):
                                for name in _stored(child.pattern):
                                    add(name, child, _start(child), "unknown", child, True, loops)
                            visit(child.body, True, loops)
                        else:
                            visit([child], True, loops)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    add(alias.asname or alias.name.split(".")[0], node, _end(node), "unknown", node, conditional, loops)

    visit(_body(owner), False, ())
    # Functions that rebind a name of ``owner`` (``global`` for the module, ``nonlocal`` for a function) can run at
    # any time: their assignments count as conditional, inside a "loop" (the function) the base is never in.
    declaration = ast.Global if module else ast.Nonlocal
    for function in ast.walk(owner):
        if function is owner or not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        declared = {name for n in ast.walk(function) if isinstance(n, declaration) for name in n.names}
        if declared:
            only = declared
            visit(function.body, True, (function,))
            only = None
    return events


def _loops_of(owner: ast.AST) -> list[ast.AST]:
    """The loops directly in ``owner`` (not in nested functions)."""
    found: list[ast.AST] = []
    stack: list[ast.AST] = list(_body(owner))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, _LOOPS):
            found.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return found


class _Scope:
    """Where names are resolved: the enclosing function (if any) and the module."""

    def __init__(
        self,
        module: ast.Module,
        function: ast.AST | None,
        consumed: set[int],
        names: set[int],
        cache: dict[tuple[int, str], Any] | None = None,
    ) -> None:
        self.module = module
        self.function = function
        self.consumed = consumed
        #: The ``Name`` nodes read while rebuilding a string (their value became part of it).
        self.names = names
        self.cache: dict[tuple[int, str], Any] = {} if cache is None else cache

    def _events(self, owner: ast.AST, name: str) -> list[_Event]:
        key = (id(owner), "\0events")
        if key not in self.cache:
            self.cache[key] = _collect_events(owner, owner is self.module)
        events: list[_Event] = self.cache[key].get(name, [])
        return events

    def _loops(self, owner: ast.AST) -> list[ast.AST]:
        key = (id(owner), "\0loops")
        if key not in self.cache:
            self.cache[key] = _loops_of(owner)
        loops: list[ast.AST] = self.cache[key]
        return loops

    def _is_parameter(self, name: str) -> bool:
        if not isinstance(self.function, _FUNCTIONS):
            return False
        args = self.function.args
        params = [*args.posonlyargs, *args.args, *args.kwonlyargs]
        if args.vararg:
            params.append(args.vararg)
        if args.kwarg:
            params.append(args.kwarg)
        return any(p.arg == name for p in params)

    def _parameter_event(self, name: str) -> _Event:
        """A parameter's value on entry: unknown (any caller passes any text), the first assignment of the name."""
        assert self.function is not None
        return _Event(_start(self.function), "unknown", self.function, False, (), self.function)

    def _local_events(self, name: str, position: Position) -> tuple[list[_Event], list[_Event], list[ast.AST]] | None:
        """The assignments of the enclosing function that can reach ``position``: those before it, and those after
        it in a loop around it (the next pass reads them); with the loops around ``position``. A parameter starts
        with an unknown value. ``None``: the name is neither a parameter nor assigned in the function."""
        if self.function is None:
            return None
        events = self._events(self.function, name)
        if self._is_parameter(name):
            events = [self._parameter_event(name), *events]
        around = [loop for loop in self._loops(self.function) if _inside(position, loop)]
        before = [e for e in events if e.position <= position]
        after = [
            dataclasses.replace(e, conditional=True)
            for e in events
            if e.position > position and any(loop in around for loop in e.loops)
        ]
        if not before and not after:
            return None
        return before, after, around

    def _declares_global(self, name: str) -> bool:
        if self.function is None:
            return False
        return any(isinstance(n, ast.Global) and name in n.names for n in ast.walk(self.function))

    def _enclosing(self, name: str) -> _Scope | None:
        """The nearest enclosing function that binds ``name`` (a closure or lambda reads it from there)."""
        if self.function is None or self._declares_global(name):
            return None
        key = (id(self.module), "\0parents")
        if key not in self.cache:
            self.cache[key] = _parents(self.module)
        parents: dict[int, ast.AST | None] = self.cache[key]
        current = parents.get(id(self.function))
        while current is not None:
            if isinstance(current, _FUNCTIONS):
                scope = _Scope(self.module, current, self.consumed, self.names, self.cache)
                if scope._is_parameter(name) or scope._events(current, name):
                    return scope
            current = parents.get(id(current))
        return None

    def _closure_values(self, name: str, depth: int) -> list[list[Any]] | None:
        """Every value ``name`` of this function can hold when a closure reads it: the closure may run at any time,
        so every assignment may be the last one and every ``+=`` may have happened or not."""
        assert self.function is not None
        events = [dataclasses.replace(e, conditional=True) for e in self._events(self.function, name)]
        if self._is_parameter(name):
            events = [self._parameter_event(name), *events]
        return self._variants(events, [], [], name, depth)

    def resolve(self, name: str, position: Position, depth: int) -> list[list[Any]] | None:
        local = self._local_events(name, position)
        if local is not None:
            before, after, around = local
            return self._variants(before, after, around, name, depth)
        enclosing = self._enclosing(name)
        if enclosing is not None:
            return enclosing._closure_values(name, depth)
        events = self._events(self.module, name)
        if events:
            return self._variants(events, [], [], name, depth)
        return [[Hole(name)]]

    def _variants(
        self, before: list[_Event], after: list[_Event], around: list[ast.AST], name: str, depth: int
    ) -> list[list[Any]] | None:
        """Every value ``name`` can hold: each assignment that may be the last one, followed by each combination of
        the ``+=`` after it (a conditional one taken and not taken). ``None``: more than :data:`MAX_VARIANTS`."""
        # The last unconditional assignment before the use starts the chain; earlier ones are overwritten.
        start = 0
        for index, event in enumerate(before):
            if event.kind != "add" and not event.conditional:
                start = index
        before = before[start:]
        if not any(e.kind != "add" for e in (*before, *after)):
            return [[Hole(name)]]
        variants: list[list[Any]] = []
        for index, base in enumerate(before):
            if base.kind == "add":
                continue
            chains = self._chain(base, [*before[index + 1 :], *after], name, depth)
            if chains is None:
                return None
            variants.extend(chains)
            if len(variants) > MAX_VARIANTS:
                return None
        for index, base in enumerate(after):
            if base.kind == "add":
                continue
            # Assigned after the use in a loop around it: the next pass runs the rest of this one, then the part of
            # the loop before the use.
            shared = [loop for loop in around if loop in base.loops]
            again = [e for e in before if any(loop in shared for loop in e.loops)]
            chains = self._chain(base, [*after[index + 1 :], *again], name, depth)
            if chains is None:
                return None
            variants.extend(chains)
            if len(variants) > MAX_VARIANTS:
                return None
        return variants

    def _chain(self, base: _Event, following: list[_Event], name: str, depth: int) -> list[list[Any]] | None:
        """The values of ``name`` from ``base`` on, through the ``+=`` that follow it until the next unconditional
        assignment."""
        heads = self._head(base, name, depth)
        if heads is None:
            return None
        base_loops = (*base.loops, base.node) if base.kind == "for" else base.loops
        chains = heads
        for event in following:
            if event.kind != "add":
                if event.conditional:
                    continue  # a conditional assignment may not happen: the chain goes on (its own chain too)
                break
            added: list[list[Any]] | None
            if not all(any(loop is outer for outer in base_loops) for loop in event.loops):
                added = [[Hole(f"{name} += ...")]]  # repeated by a loop the base is not in: any number of times
            else:
                assert isinstance(event.value, ast.expr)
                added = self.render(event.value, _start(event.value), depth + 1)
                if added is None:
                    return None
            options = [*added, []] if event.conditional else added
            combined = _product([chains, options])
            if combined is None:
                return None
            chains = combined
        return chains

    def _head(self, base: _Event, name: str, depth: int) -> list[list[Any]] | None:
        if base.kind == "unknown":
            return [[Hole(name)]]
        if base.kind == "for":
            return self._iterate(base.value, _start(base.value), depth)
        if base.loops and _binds(base.value, name):
            return [[Hole(name)]]  # `q = q + ...` in a loop (or in a function, for a global): grows every time
        assert isinstance(base.value, ast.expr)
        return self.render(base.value, _start(base.value), depth + 1)

    def _iterate(self, iterable: ast.AST, position: Position, depth: int) -> list[list[Any]] | None:
        if isinstance(iterable, ast.Name):
            self.names.add(id(iterable))
            events = self._events(self.module, iterable.id)
            if len(events) == 1 and events[0].kind == "set":
                iterable = events[0].value
        if isinstance(iterable, (ast.Tuple, ast.List)):
            options: list[list[Any]] = []
            for element in iterable.elts:
                rendered = self.render(element, position, depth + 1)
                if rendered is None:
                    return None
                options.extend(rendered)
            return options
        return [[Hole(ast.unparse(iterable))]] if isinstance(iterable, ast.expr) else None

    def render(self, node: ast.expr, position: Position | int, depth: int = 0) -> list[list[Any]] | None:
        """The possible texts of ``node`` as lists of pieces (str or :class:`Hole`), with names resolved as they are at
        ``position`` (a line alone counts as its end); ``None``: too many variants."""
        if isinstance(position, int):
            position = (position, 1 << 30)
        self.consumed.add(id(node))
        if depth > 12:
            return [[Hole(ast.unparse(node))]]
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                return [[node.value]]
            if isinstance(node.value, bytes):
                try:
                    return [[node.value.decode("utf-8")]]
                except UnicodeDecodeError:
                    return [[Hole(ast.unparse(node))]]
            return [[Hole(ast.unparse(node))]]
        if isinstance(node, ast.JoinedStr):
            parts: list[list[list[Any]]] = []
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append([[value.value]])
                elif isinstance(value, ast.FormattedValue):
                    if value.conversion != -1 or value.format_spec is not None:
                        parts.append([[Hole(ast.unparse(value.value))]])
                    else:
                        rendered = self.render(value.value, position, depth + 1)
                        if rendered is None:
                            return None
                        parts.append(rendered)
                else:
                    parts.append([[Hole(ast.unparse(value))]])
            return _product(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self.render(node.left, position, depth + 1)
            right = self.render(node.right, position, depth + 1)
            if left is None or right is None:
                return None
            return _product([left, right])
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            return self._render_percent(node, position, depth)
        if isinstance(node, ast.Subscript):
            return self._render_item(node, position, depth)
        if isinstance(node, ast.IfExp):
            body = self.render(node.body, position, depth + 1)
            orelse = self.render(node.orelse, position, depth + 1)
            if body is None or orelse is None:
                return None
            return [*body, *orelse]
        if isinstance(node, ast.Name):
            self.names.add(id(node))
            return self.resolve(node.id, position, depth + 1)
        if isinstance(node, ast.Call):
            return self._render_call(node, position, depth)
        return [[Hole(ast.unparse(node))]]

    def _render_call(self, node: ast.Call, position: Position, depth: int) -> list[list[Any]] | None:
        func = node.func
        # sql.SQL("..."), sql.Identifier("a", "b"), sql.Placeholder(), sql.Literal(x), sql.Composed([...])
        if _is_sql_attr(func, "SQL") and len(node.args) == 1 and not node.keywords:
            return self.render(node.args[0], position, depth + 1)
        if _is_sql_attr(func, "Identifier") and node.args and not node.keywords:
            parts: list[list[list[Any]]] = []
            for index, arg in enumerate(node.args):
                rendered = self.render(arg, position, depth + 1)
                if rendered is None:
                    return None
                quoted = [
                    ['"', *variant, '"'] if all(isinstance(p, str) for p in variant) else [Hole(ast.unparse(arg))]
                    for variant in rendered
                ]
                parts.append(quoted if index == 0 else [[".", *q] for q in quoted])
            return _product(parts)
        if _is_sql_attr(func, "Placeholder"):
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                return [[f"%({node.args[0].value})s"]]
            return [["%s"]] if not node.args else [[Hole(ast.unparse(node))]]
        if _is_sql_attr(func, "Composed") and len(node.args) == 1:
            return self._render_sequence(node.args[0], [[""]], position, depth)
        if isinstance(func, ast.Attribute) and func.attr == "join" and len(node.args) == 1 and not node.keywords:
            separator = self.render(func.value, position, depth + 1)
            if separator is None:
                return None
            return self._render_sequence(node.args[0], separator, position, depth)
        if isinstance(func, ast.Attribute) and func.attr == "format":
            template = self.render(func.value, position, depth + 1)
            if template is None:
                return None
            return self._render_format(template, node, position, depth)
        if isinstance(func, ast.Attribute) and func.attr == "as_string":
            return self.render(func.value, position, depth + 1)
        if isinstance(func, ast.Attribute) and func.attr in _SAME_TEXT | _CHANGED_TEXT:
            receiver = self.render(func.value, position, depth + 1)
            if receiver is None:
                return None
            if func.attr in ("encode", "decode") or (func.attr in _SAME_TEXT and not node.args and not node.keywords):
                return receiver
            return [[*variant, Hole(f".{func.attr}(...)")] for variant in receiver]
        return [[Hole(ast.unparse(node))]]

    def _render_percent(self, node: ast.BinOp, position: Position, depth: int) -> list[list[Any]] | None:
        """``template % values``: each ``%s`` replaced by the text of its value (a literal tuple or dict, or one value);
        any other conversion, or a value that cannot be matched to it, is an unknown part in its place."""
        left = self.render(node.left, position, depth + 1)
        if left is None:
            return None
        right = node.right
        positional: list[ast.expr] | None = None
        keyed: dict[str, ast.expr] | None = None
        if isinstance(right, ast.Tuple) and not any(isinstance(e, ast.Starred) for e in right.elts):
            positional = list(right.elts)
        elif isinstance(right, ast.Dict) and all(
            isinstance(k, ast.Constant) and isinstance(k.value, str) for k in right.keys
        ):
            keyed = {
                k.value: v
                for k, v in zip(right.keys, right.values, strict=True)
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
        elif not isinstance(right, (ast.Tuple, ast.Dict, ast.List, ast.Set, ast.Starred)):
            positional = [right]
        unknown = Hole(f"% {ast.unparse(right)}")
        results: list[list[Any]] = []
        for variant in left:
            matchable = not any(isinstance(p, Hole) for p in variant)
            parts: list[list[list[Any]]] = []
            index = 0
            for piece in variant:
                if isinstance(piece, Hole):
                    parts.append([[piece]])
                    continue
                last = 0
                for match in _PERCENT.finditer(piece):
                    parts.append([[piece[last : match.start()]]])
                    last = match.end()
                    if match.group("conversion") == "%":
                        parts.append([["%"]])
                        continue
                    key = match.group("key")
                    argument: ast.expr | None = None
                    if matchable and key is not None and keyed is not None:
                        argument = keyed.get(key)
                    elif matchable and key is None and positional is not None:
                        argument = positional[index] if index < len(positional) else None
                    index += 1
                    simple = match.group("conversion") == "s" and not match.group("format")
                    if argument is None or not simple:
                        parts.append([[unknown]])
                        continue
                    rendered = self.render(argument, position, depth + 1)
                    if rendered is None:
                        return None
                    parts.append(rendered)
                parts.append([[piece[last:]]])
            combined = _product(parts)
            if combined is None:
                return None
            results.extend(combined)
            if len(results) > MAX_VARIANTS:
                return None
        return results

    def _render_item(self, node: ast.Subscript, position: Position, depth: int) -> list[list[Any]] | None:
        """``NAME[constant]`` of a tuple, list or dict literal that nothing changes: the text of that item (which is
        still checked on its own too); anything else is an unknown part."""
        unknown: list[list[Any]] = [[Hole(ast.unparse(node))]]
        container, index = node.value, node.slice
        if not isinstance(container, ast.Name) or not isinstance(index, ast.Constant):
            return unknown
        self.names.add(id(container))
        name = container.id
        if self._is_parameter(name):
            return unknown
        owner: ast.AST = self.module
        if self.function is not None and self._events(self.function, name):
            owner = self.function
        events = self._events(owner, name)
        if not events or any(e.kind != "set" for e in events):
            return unknown
        cache_key = (id(self.module), f"\0mutated {name}")
        if cache_key not in self.cache:
            self.cache[cache_key] = _mutated(self.module, name)
        if self.cache[cache_key]:
            return unknown
        items: list[ast.expr] = []
        for event in events:
            value = event.value
            item: ast.expr | None = None
            if isinstance(value, (ast.Tuple, ast.List)) and isinstance(index.value, int):
                count = len(value.elts)
                if not any(isinstance(e, ast.Starred) for e in value.elts) and -count <= index.value < count:
                    item = value.elts[index.value]
            elif isinstance(value, ast.Dict) and None not in value.keys:
                for key, candidate in zip(value.keys, value.values, strict=True):
                    if isinstance(key, ast.Constant) and key.value == index.value:
                        item = candidate
            if item is None:
                return unknown
            items.append(item)
        variants: list[list[Any]] = []
        before = set(self.consumed)
        for item in items:
            rendered = self.render(item, _start(item), depth + 1)
            if rendered is None:
                return None
            variants.extend(rendered)
        # The item is read here, not consumed: where it is written, it is checked on its own as well.
        self.consumed.intersection_update(before)
        return variants if len(variants) <= MAX_VARIANTS else None

    def _render_sequence(
        self, sequence: ast.expr, separator: list[list[Any]], position: Position, depth: int
    ) -> list[list[Any]] | None:
        if isinstance(sequence, ast.Name):
            return self._render_named_sequence(sequence, separator, position, depth)
        if not isinstance(sequence, (ast.List, ast.Tuple)) or any(isinstance(e, ast.Starred) for e in sequence.elts):
            return [[Hole(ast.unparse(sequence))]]
        parts: list[list[list[Any]]] = []
        for index, element in enumerate(sequence.elts):
            if index:
                parts.append(separator)
            rendered = self.render(element, position, depth + 1)
            if rendered is None:
                return None
            parts.append(rendered)
        return _product(parts) if parts else [[]]

    def _render_named_sequence(
        self, sequence: ast.Name, separator: list[list[Any]], position: Position, depth: int
    ) -> list[list[Any]] | None:
        """A list or tuple held by a name: each list it is assigned, followed by an unknown part when it is changed
        (``append``, ``+=``, an item assigned, passed to a call ...); unknown when it is assigned anything else."""
        self.names.add(id(sequence))
        name = sequence.id
        owner: ast.AST = self.module
        if self._is_parameter(name):
            return [[Hole(name)]]
        if self.function is not None and self._events(self.function, name):
            owner = self.function
        events = self._events(owner, name)
        if not events or any(e.kind not in ("set", "add") for e in events):
            return [[Hole(name)]]
        bases = [e for e in events if e.kind == "set"]
        if not bases or not all(isinstance(e.value, (ast.List, ast.Tuple)) for e in bases):
            return [[Hole(name)]]
        changed = len(bases) != len(events) or _changed_elsewhere(owner, name, sequence)
        variants: list[list[Any]] = []
        for base in bases:
            assert isinstance(base.value, ast.expr)
            rendered = self._render_sequence(base.value, separator, _start(base.value), depth + 1)
            if rendered is None:
                return None
            if changed:
                tail = _product([rendered, separator, [[Hole(f"{name} (changed)")]]])
                if tail is None:
                    return None
                rendered = tail
            variants.extend(rendered)
            if len(variants) > MAX_VARIANTS:
                return None
        return variants

    def _render_format(
        self, template: list[list[Any]], call: ast.Call, position: Position, depth: int
    ) -> list[list[Any]] | None:
        if any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
            return [[*variant, Hole(ast.unparse(call))] for variant in template]
        keywords = {k.arg: k.value for k in call.keywords if k.arg is not None}
        results: list[list[Any]] = []
        for variant in template:
            parts: list[list[list[Any]]] = []
            auto = 0
            for piece in variant:
                if isinstance(piece, Hole):
                    parts.append([[piece]])
                    continue
                try:
                    parsed = list(string.Formatter().parse(piece))
                except ValueError:
                    parts.append([[Hole(f"invalid format string {piece!r}")]])
                    continue
                for literal, field_name, spec, conversion in parsed:
                    if literal:
                        parts.append([[literal]])
                    if field_name is None:
                        continue
                    argument: ast.expr | None = None
                    if field_name == "":
                        argument = call.args[auto] if auto < len(call.args) else None
                        auto += 1
                    elif field_name.isdigit():
                        index = int(field_name)
                        argument = call.args[index] if index < len(call.args) else None
                    else:
                        argument = keywords.get(field_name)
                    if argument is None or spec or conversion:
                        parts.append([[Hole(field_name or "{}")]])
                        continue
                    rendered = self.render(argument, position, depth + 1)
                    if rendered is None:
                        return None
                    parts.append(rendered)
            combined = _product(parts)
            if combined is None:
                return None
            results.extend(combined)
            if len(results) > MAX_VARIANTS:
                return None
        return results


def _parents(module: ast.Module) -> dict[int, ast.AST | None]:
    """The nearest enclosing function or class of every function and class of ``module`` (``None``: the module)."""
    parents: dict[int, ast.AST | None] = {}

    def visit(node: ast.AST, owner: ast.AST | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (*_FUNCTIONS, ast.ClassDef)):
                parents[id(child)] = owner
                visit(child, child)
            else:
                visit(child, owner)

    visit(module, None)
    return parents


def _changed_elsewhere(owner: ast.AST, name: str, use: ast.Name) -> bool:
    """Whether the list held by ``name`` may be changed in ``owner``: any other read of the name (a method call, an
    item assigned or deleted, an argument of a call ...) or a ``global`` declaration counts."""
    for node in ast.walk(owner):
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load) and node is not use:
            return True
        if isinstance(node, ast.Global) and name in node.names:
            return True
    return False


#: A ``%`` conversion of Python's ``str % values`` (``%%`` included).
_PERCENT = re.compile(
    r"%(?:\((?P<key>[^)]*)\))?(?P<format>[#0\- +]*(?:\*|\d+)?(?:\.(?:\*|\d+))?)[hlL]?(?P<conversion>.)", re.S
)
#: Methods that change a list or dict in place.
_MUTATORS = frozenset(
    {"append", "extend", "insert", "pop", "remove", "clear", "sort", "reverse", "update", "setdefault", "popitem"}
)


def _mutated(module: ast.Module, name: str) -> bool:
    """Whether a container held by ``name`` may be changed anywhere in ``module``: an item assigned, deleted or
    extended, a changing method called, the name passed to a call or rebound with ``global``."""
    for node in ast.walk(module):
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == name:
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                return True
        elif isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == name
                and func.attr in _MUTATORS
            ):
                return True
            arguments = [*node.args, *(k.value for k in node.keywords)]
            if any(isinstance(a, ast.Name) and a.id == name for a in arguments):
                return True
        elif isinstance(node, ast.Global) and name in node.names:
            return True
    return False


def _binds(target: ast.AST, name: str) -> bool:
    return any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(target))


def _is_item(node: ast.AST) -> bool:
    return isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and isinstance(node.slice, ast.Constant)


def _is_string_root(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (str, bytes))
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) and isinstance(node.left, ast.Name):
        return True  # ``QUERY % values``: the template's text with its values
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        if isinstance(node.op, ast.Add) and any(_is_item(side) for side in (node.left, node.right)):
            return True  # ``QS[0] + QS[1]``: the items of a literal container, put together
        return _is_string_root(node.left) or _is_string_root(node.right)
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in (
            "format",
            "join",
            "as_string",
            *_SAME_TEXT,
            *_CHANGED_TEXT,
        ):
            # On a name too: ``QUERY.replace(...)`` or ``template.format(...)`` is a new text, checked as such.
            return isinstance(func.value, ast.Name) or _is_string_root(func.value)
        return any(_is_sql_attr(func, name) for name in ("SQL", "Composed"))
    return False


def _docstring_ids(tree: ast.Module) -> set[int]:
    """Bare string statements (docstrings and comments in quotes): never executed."""
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, (ast.Constant, ast.JoinedStr))
    }


def _roots(tree: ast.Module) -> Iterator[tuple[ast.expr, ast.AST | None]]:
    """The outermost string expressions of a module with their enclosing function."""
    skip = _docstring_ids(tree)

    def visit(node: ast.AST, function: ast.AST | None) -> Iterator[tuple[ast.expr, ast.AST | None]]:
        for child in ast.iter_child_nodes(node):
            if id(child) in skip:
                continue
            if isinstance(child, ast.expr) and _is_string_root(child):
                yield child, function
                # Arguments of .format()/sql calls are part of the root; other nested strings are their own roots.
                continue
            inner = child if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) else function
            yield from visit(child, inner)

    yield from visit(tree, None)


def _text(pieces: list[Any]) -> tuple[str, bool]:
    text = "".join(p if isinstance(p, str) else "{" + p.source + "}" for p in pieces)
    return " ".join(text.split()), any(isinstance(p, Hole) for p in pieces)


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


def _starts_like_data(text: str) -> bool:
    """Whether ``text`` starts with a keyword of a statement that reads or changes rows. ``TABLE`` counts only in
    capitals (``Table 3`` is a caption, not a statement)."""
    stripped = text.lstrip(" (\n\t")
    word = []
    for char in stripped:
        if not (char.isalnum() or char == "_"):
            break
        word.append(char)
    first = "".join(word)
    return first.lower() in _DATA_KEYWORDS and (first.lower() != "table" or first == "TABLE")


def _builds_table_name(pieces: list[Any]) -> bool:
    """A table name completed at run time: ``nodestro_`` (lower case, as tables are named in code) followed by
    identifier characters and then an unknown part, as in ``f"nodestro_{kind}"``."""
    for index, piece in enumerate(pieces[:-1]):
        if not isinstance(piece, str) or not isinstance(pieces[index + 1], Hole):
            continue
        tail = []
        for char in reversed(piece):
            if not (char.isalnum() or char == "_"):
                break
            tail.append(char)
        if "".join(reversed(tail)).startswith("nodestro_"):
            return True
    return False


def _name_uses(tree: ast.Module) -> Iterator[tuple[ast.Name, ast.AST | None]]:
    """Every read of a name with its enclosing function."""

    def visit(node: ast.AST, function: ast.AST | None) -> Iterator[tuple[ast.Name, ast.AST | None]]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                yield child, function
            inner = child if isinstance(child, _FUNCTIONS) else function
            yield from visit(child, inner)

    yield from visit(tree, None)


#: SQL keywords that the known part of a text after an unknown part may hold.
_KEYWORDS = (
    r"SELECT|INSERT|UPDATE|DELETE|FROM|WHERE|JOIN|AND|OR|NOT|LIMIT|OFFSET|ORDER\s+BY|GROUP\s+BY|HAVING|UNION"
    r"|INTERSECT|EXCEPT|RETURNING|VALUES|SET|ON\s+CONFLICT"
)
#: SQL words in capitals (as this code base writes SQL), or ``tenant_id``.
_SQL_WORDS = re.compile(rf"\b(?:{_KEYWORDS}|tenant_id)\b")
#: SQL in lower case: only a keyword in a SQL shape counts (``or true``, ``and id = %s``, ``union select``), since
#: ``or``, ``and``, ``not`` and ``where`` are English too (``f"{name} must be yes or no"``).
_SQL_LOWER_CASE = re.compile(
    r"\b(?:or|and|not|where)\s+(?:true|false|null|%s|%\(\w+\)s|\$\d"
    r"|[\w.]+\s*(?:=|<>|!=|<|>|::|\bis\b|\bin\b|\blike\b|\bilike\b))"
    r"|\bunion\s+(?:all\s+)?select\b|\blimit\s+(?:\d|%s|\$\d)|\bon\s+conflict\b"
    r"|;\s*(?:select|insert|update|delete|drop|truncate|copy)\b",
    re.IGNORECASE,
)
#: A comment, which hides the rest of a statement (``--`` before a space or the end; ``--flag`` is a command line).
_SQL_COMMENT = re.compile(r"--(?=\s|$)|/\*")
#: The start of a text that continues a statement: a condition, a set operation, a clause or a second statement.
_CONTINUATION = re.compile(
    r"[\s()]*(?:(?:OR|AND|WHERE|UNION|INTERSECT|EXCEPT|LIMIT|OFFSET|ORDER\s+BY|GROUP\s+BY|HAVING|RETURNING|JOIN"
    r"|ON\s+CONFLICT)\b|;)",
    re.IGNORECASE,
)


def _known(pieces: list[Any]) -> str:
    """The known parts of a text as Python joins them (adjacent constants without a gap: ``"nodestro_" +
    "documents"`` is one name), an unknown part as a space."""
    return "".join(p if isinstance(p, str) else " " for p in pieces)


def _sql_like(text: str) -> bool:
    """Whether ``text`` (known parts only) holds SQL: a keyword in capitals, a comment, or a keyword in lower case in a
    SQL shape."""
    return bool(_SQL_WORDS.search(text) or _SQL_COMMENT.search(text) or _SQL_LOWER_CASE.search(text))


def _hole_led_sql(pieces: list[Any]) -> bool:
    """Whether a text starts with an unknown part (an imported fragment, ``self.BASE``, a function's result, a
    parameter) and its known parts hold SQL: its statement is not known, so it is reported."""
    for piece in pieces:
        if isinstance(piece, str) and not piece.strip(" (\n\t"):
            continue
        if not isinstance(piece, Hole):
            return False
        break
    else:
        return False
    return _sql_like(_known(pieces))


def _continues_statement(pieces: list[Any]) -> bool:
    """Whether a text that is not part of a known statement continues one (``" OR true"`` in a class attribute, a
    tuple or a call argument): the statement it is appended to is not known, so it is reported."""
    if not pieces or not isinstance(pieces[0], str):
        return False
    text = _known(pieces)
    return bool(_CONTINUATION.match(text)) and _sql_like(text)


def _followed_values(tree: ast.Module) -> set[int]:
    """The values of the ``name += ...`` that the analysis follows (in a function or the module, not in a class
    body): each is part of the name's value where the name is read."""
    widened = {id(n) for n, _function in _widenings(tree)}
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) and id(node) not in widened
    }


def _widenings(tree: ast.Module) -> Iterator[tuple[ast.AugAssign, ast.AST | None]]:
    """The ``+=`` whose target is not followed as a name (an attribute, an item, a name in a class body), with the
    enclosing function: the value before it is not known."""

    def visit(
        node: ast.AST, function: ast.AST | None, in_class: bool
    ) -> Iterator[tuple[ast.AugAssign, ast.AST | None]]:
        for child in ast.iter_child_nodes(node):
            if (
                isinstance(child, ast.AugAssign)
                and isinstance(child.op, ast.Add)
                and (in_class or not isinstance(child.target, ast.Name))
            ):
                yield child, function
            if isinstance(child, _FUNCTIONS):
                yield from visit(child, child, False)
            elif isinstance(child, ast.ClassDef):
                yield from visit(child, function, True)
            else:
                yield from visit(child, function, in_class)

    yield from visit(tree, None, False)


#: The unknown part in front of a text that continues a statement (:func:`_continues_statement`).
_FRAGMENT = "statement not known"


def statements_of(source: str, module: str, tables: frozenset[str]) -> list[Statement]:
    """Every variant of every string expression in ``source`` that must be checked as SQL, and every value a name
    can hold where the name is read (``db._query(query)`` after ``query += ...``)."""
    tree = ast.parse(source)
    consumed: set[int] = set()
    names: set[int] = set()
    cache: dict[tuple[int, str], Any] = {}
    rendered: list[tuple[ast.AST, list[list[Any]] | None, bool]] = []
    for root, function in _roots(tree):
        scope = _Scope(tree, function, consumed, names, cache)
        before = set(consumed)
        result = scope.render(root, _start(root))
        consumed.discard(id(root))
        consumed |= before
        rendered.append((root, result, False))
    # `self.q += " OR ..."`, `Q["a"] += ...`, `+=` in a class body: the text before is not known, the text after is
    # checked as a text that starts with an unknown part.
    for widening, function in _widenings(tree):
        scope = _Scope(tree, function, consumed, names, cache)
        value = scope.render(widening.value, _start(widening.value))
        target = Hole(ast.unparse(widening.target))
        rendered.append((widening, None if value is None else [[target, *v] for v in value], True))
    # A name read outside a string (a bare argument, a return value ...) is resolved where it is read: every value it
    # can hold there is checked, with every `+=` and reassignment before it (and after it in a loop around it).
    assigned = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    uses = [(use, function) for use, function in _name_uses(tree) if id(use) not in names and use.id in assigned]
    for use, function in uses:
        scope = _Scope(tree, function, consumed, names, cache)
        rendered.append((use, scope.render(use, _start(use)), True))
    # The value of `name += ...` is checked where the name is read (above), not as a fragment of its own.
    followed = _followed_values(tree)
    found: list[Statement] = []
    for node, variants, is_use in rendered:
        if not is_use and id(node) in consumed:
            continue  # part of another expression or the value of a name read elsewhere: checked there
        line = getattr(node, "lineno", 0)
        if variants is None:
            found.append(Statement(module, line, " ".join(ast.unparse(node).split()), True))
            continue
        for pieces in variants:
            # Only the known parts count: an unknown part's source text (``{db._query('SELECT ...')}``) holds strings
            # that are checked on their own.
            names_table = bool(_words(_known(pieces)) & tables)
            if not names_table and id(node) not in followed and _continues_statement(pieces):
                # `" OR true"` on its own (a class attribute, a tuple item, a call argument): it continues a statement
                # that is not known here.
                pieces = [Hole(_FRAGMENT), *pieces]
            text, dynamic = _text(pieces)
            if dynamic:
                if not (names_table or _builds_table_name(pieces) or _starts_like_data(text) or _hole_led_sql(pieces)):
                    continue
            elif not names_table:
                continue
            found.append(Statement(module, line, text, dynamic))
    # The same variant twice in one expression (e.g. both branches of an if) is one statement.
    return list(dict.fromkeys(found))


# --- SQL side: check a statement with PostgreSQL's parser -------------------------------------------------------


def to_postgres_params(text: str) -> str:
    """psycopg's placeholders (``%s``, ``%(name)s``, ``%b``, ``%t``) as ``$n``; ``%%`` as ``%``."""
    out: list[str] = []
    names: dict[str, int] = {}
    count = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char != "%":
            out.append(char)
            index += 1
            continue
        following = text[index + 1 : index + 2]
        if following == "%":
            out.append("%")
            index += 2
        elif following in ("s", "b", "t"):
            count += 1
            out.append(f"${count}")
            index += 2
        elif following == "(":
            end = text.find(")", index)
            if end == -1 or text[end + 1 : end + 2] not in ("s", "b", "t"):
                out.append(char)
                index += 1
                continue
            name = text[index + 2 : end]
            if name not in names:
                count += 1
                names[name] = count
            out.append(f"${names[name]}")
            index = end + 2
        else:
            out.append(char)
            index += 1
    return "".join(out)


@dataclass
class _Ref:
    name: str  # alias or table name, as columns qualify it
    table: str
    bound: bool = False


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


def _tenant_column(expr: Any) -> tuple[str, ...] | None:
    """The qualifier of a ``tenant_id`` column reference (``()`` when unqualified), else ``None``."""
    pg = _pglast("ast")

    while isinstance(expr, pg.TypeCast):
        expr = expr.arg
    if not isinstance(expr, pg.ColumnRef):
        return None
    names = tuple(getattr(f, "sval", None) for f in expr.fields)
    if not names or names[-1] != "tenant_id" or any(n is None for n in names):
        return None
    return tuple(str(n) for n in names[:-1])


class _Checker:
    def __init__(self, tables: frozenset[str]) -> None:
        self.tables = tables
        self.problems: list[str] = []

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
                    ref = _Ref(item.alias.aliasname if item.alias else item.relname, table)
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
        """Mark refs bound by ``tenant_id = param``/``= ANY(param)`` or by equality with a bound ref. A qualifier means
        the nearest name: this statement's own (which hides an outer one of the same name), then the enclosing
        statements' from the inside out."""
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
                    for column, other in sides:
                        qualifier = _tenant_column(column)
                        if qualifier is None:
                            continue
                        ref = lookup(qualifier, applies)
                        if ref is None or ref.bound:
                            continue
                        if _is_param(other):
                            ref.bound = changed = True
                            continue
                        other_qualifier = _tenant_column(other)
                        if conjunct.kind != A_Expr_Kind.AEXPR_OP or other_qualifier is None or not other_qualifier:
                            continue
                        if partner_binds(other_qualifier[-1], ref):
                            ref.bound = changed = True

    def _check_insert(self, node: Any, table: str) -> None:
        """``tenant_id`` is listed and its value is a parameter (or a bound ``tenant_id``); an upsert's conflict target
        includes ``tenant_id``, so the update it turns into cannot hit another tenant's row."""
        pg = _pglast("ast")
        OnConflictAction = _pglast("enums").OnConflictAction

        columns = [c.name for c in node.cols or ()]
        if "tenant_id" not in columns:
            self.problems.append(f"the INSERT into {table} lists no tenant_id column")
            return
        index = columns.index("tenant_id")
        select = node.selectStmt
        if isinstance(select, pg.SelectStmt) and select.valuesLists:
            values = [row[index] if index < len(row) else None for row in select.valuesLists]
        elif isinstance(select, pg.SelectStmt) and select.targetList and select.larg is None:
            own, _quals, _names = self._from_refs(select.fromClause, set())
            target = select.targetList[index].val if index < len(select.targetList) else None
            qualifier = _tenant_column(target)
            from_own = qualifier is not None and (
                [r for r in own if r.name == qualifier[-1]] if qualifier else own[:1] if len(own) == 1 else []
            )
            values = [None if not from_own and not _is_param(target) else target]
        else:
            values = [None]
        if not all(value is not None and (_is_param(value) or _tenant_column(value) is not None) for value in values):
            self.problems.append(f"the tenant_id written into {table} is not a parameter or a bound tenant_id")
        clause = node.onConflictClause
        if clause is not None and clause.action == OnConflictAction.ONCONFLICT_UPDATE:
            infer = clause.infer
            target_columns = [e.name for e in (infer.indexElems or ())] if infer is not None else []
            if "tenant_id" not in target_columns:
                self.problems.append(
                    f"the conflict target of an upsert into {table} lacks tenant_id: it may update another tenant's row"
                )
            for item in clause.targetList or ():
                if item.name == "tenant_id" and not (
                    "tenant_id" in target_columns and _tenant_column(item.val) == ("excluded",)
                ):
                    self.problems.append(f"the upsert into {table} sets tenant_id: it moves a row to another tenant")

    def check(self, node: Any, outer: list[tuple[set[str], list[_Ref]]], ctes: set[str]) -> None:
        pg = _pglast("ast")

        own: list[_Ref] = []
        names: set[str] = set()
        if isinstance(node, (pg.SelectStmt, pg.UpdateStmt, pg.DeleteStmt, pg.InsertStmt)):
            with_clause = getattr(node, "withClause", None)
            if with_clause is not None:
                for cte in with_clause.ctes or ():
                    if cte.ctename in self.tables:
                        self.problems.append(f"CTE named like the tenant table {cte.ctename}")
                    ctes = ctes | {cte.ctename}
        if isinstance(node, pg.SelectStmt):
            own, quals, names = self._from_refs(node.fromClause, ctes)
            self._bind(own, [(node.whereClause, own), *quals], outer, names)
        elif isinstance(node, (pg.UpdateStmt, pg.DeleteStmt)):
            target = [node.relation]
            extra = node.fromClause if isinstance(node, pg.UpdateStmt) else node.usingClause
            own, quals, names = self._from_refs([*target, *(extra or ())], ctes)
            self._bind(own, [(node.whereClause, own), *quals], outer, names)
            if (
                isinstance(node, pg.UpdateStmt)
                and node.relation.relname.lower() in self.tables
                and any(item.name == "tenant_id" for item in node.targetList or ())
            ):
                self.problems.append(
                    f"the UPDATE of {node.relation.relname} sets tenant_id: it moves a row to another tenant"
                )
        elif isinstance(node, pg.InsertStmt):
            table = node.relation.relname
            if table.lower() in self.tables:
                self._check_insert(node, table)
        for ref in own:
            if not ref.bound:
                self.problems.append(
                    f"{ref.table}"
                    + (f" (as {ref.name})" if ref.name != ref.table else "")
                    + " is not bound to one tenant by a top-level `tenant_id = <param>` conjunct"
                )
        for child in _children(node):
            self.check(child, [*outer, (names, own)], ctes)


#: A (possibly qualified) name: a string that is only a name reads no rows (``relname = 'nodestro_documents'``).
_IDENTIFIER = re.compile(r"[\w$]+(?:\.[\w$]+)*")


def check_sql(text: str, tables: frozenset[str]) -> list[str]:
    """Why ``text`` (one or more PostgreSQL statements with psycopg placeholders) may read or change another tenant's
    rows; empty when every reference to a tenant table is bound to one tenant. Raises ``pglast.parser.ParseError``."""
    parse_sql = _pglast("").parse_sql

    problems: list[str] = []
    for raw in parse_sql(to_postgres_params(text)):
        stmt = raw.stmt
        kind = type(stmt).__name__
        # SQL in a string (a DO block, a function body, query_to_xml('...')) is run by the server, not checked here.
        for node in _walk(stmt):
            value = getattr(node, "sval", None) if type(node).__name__ == "String" else None
            if isinstance(value, str) and _words(value) & tables and not _IDENTIFIER.fullmatch(value):
                problems.append(f"a string that names a tenant table ({value[:80]!r}) is not supported by the check")
        if kind in _DATA_STATEMENTS or kind == "ExplainStmt":
            checker = _Checker(tables)
            checker.check(stmt, [], set())
            problems.extend(checker.problems)
            continue
        touched = sorted(
            {n.relname for n in _walk(stmt) if type(n).__name__ == "RangeVar" and n.relname.lower() in tables}
        )
        if not touched:
            continue
        if kind in _SCHEMA_STATEMENTS:
            continue
        problems.append(f"{kind} on {', '.join(touched)} is not supported by the check")
    return problems


# --- packages ------------------------------------------------------------------------------------------------------


#: The module of a package that holds its lists of exceptions (data, never executed).
ALLOWLIST_MODULE = "tenant_sql_allowlist.py"


def _only_lists(source: str) -> bool:
    """Whether ``source`` consists only of a docstring, ``from __future__`` imports and the assignment of the two
    lists (dict literals with string keys and values)."""
    for index, node in enumerate(ast.parse(source).body):
        if index == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id in ("ALLOWLIST", "SKIP_MODULES")
            and isinstance(node.value, ast.Dict)
            and all(
                isinstance(n, (ast.Constant, ast.Tuple))
                for n in [*node.value.keys, *node.value.values]
                if n is not None
            )
        ):
            continue
        return False
    return True


def scan_package(
    root: Path,
    tables: frozenset[str],
    allowlist: Mapping[tuple[str, str], str],
    skip_modules: Mapping[str, str] | None = None,
) -> Report:
    """Check every ``.py`` file under ``root`` (a package directory). Modules are named relative to ``root``'s parent
    (``nodestro_runtime/jobs.py``); ``allowlist`` maps ``(module, normalized text)`` to its reason; ``skip_modules``
    maps a module that holds no PostgreSQL statements (the desktop's SQLite store) to the reason."""
    from pglast.parser import ParseError

    skip_modules = skip_modules or {}
    report = Report()
    prefix = root.name + "/"
    used: set[tuple[str, str]] = set()
    for path in sorted(root.rglob("*.py")):
        module = path.relative_to(root.parent).as_posix()
        if path.name == ALLOWLIST_MODULE:
            # The lists of exceptions hold statement texts as data. They may hold nothing else.
            if not _only_lists(path.read_text(encoding="utf-8")):
                report.findings.append(
                    Finding(module, 0, "skip", "", f"{ALLOWLIST_MODULE} may only assign ALLOWLIST and SKIP_MODULES")
                )
            continue
        if module in skip_modules:
            source = path.read_text(encoding="utf-8")
            if "sqlite3" not in source or "psycopg" in source or "PostgresDatabase" in source:
                report.findings.append(
                    Finding(module, 0, "skip", "", "a skipped (SQLite) module must use sqlite3 and never PostgreSQL")
                )
            continue
        for statement in statements_of(path.read_text(encoding="utf-8"), module, tables):
            key = (module, statement.text)
            if key in allowlist:
                used.add(key)
                report.allowed.append(statement)
                continue
            report.checked.append(statement)
            if statement.dynamic:
                if statement.text.startswith("{" + _FRAGMENT + "}"):
                    detail = "SQL that continues a statement that is not known"
                elif statement.text.lstrip(" (").startswith("{"):
                    detail = "SQL that starts with a part that is not known"
                else:
                    detail = "SQL with a part that is not known"
                report.findings.append(Finding(module, statement.line, "dynamic", statement.text, detail))
                continue
            try:
                problems = check_sql(statement.text, tables)
            except ParseError as exc:
                report.findings.append(Finding(module, statement.line, "unparsed", statement.text, str(exc)))
                continue
            for problem in problems:
                kind = "unsupported" if "not supported" in problem else "leak"
                report.findings.append(Finding(module, statement.line, kind, statement.text, problem))
    for module in skip_modules:
        if module.startswith(prefix) and not (root.parent / module).is_file():
            report.findings.append(Finding(module, 0, "stale", "", "skipped module does not exist"))
    for key in allowlist:
        if key[0].startswith(prefix) and key not in used:
            report.findings.append(Finding(key[0], 0, "stale", key[1], "allowlist entry matches no statement"))
    return report
