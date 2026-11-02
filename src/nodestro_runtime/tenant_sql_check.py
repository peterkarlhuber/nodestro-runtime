"""Static check that every PostgreSQL statement on a tenant table is bound to one tenant (security review SR-01).

Nodestro's own tables (``nodestro_*``) have no row-level security: a store is bound to one tenant and every statement
filters on (or writes) ``tenant_id`` (``docs/AXENTRA.md``, "Row-level security on Nodestro's tables"). This module is
the control that keeps it so. It is used by the test suites of nodestro-runtime and nodestro-api; nothing imports it at
run time, and its parser (``pglast``, PostgreSQL's own grammar) is a test dependency only.

How it works:

- :func:`tenant_tables` reads the migrations and returns every ``nodestro_*`` table with a ``tenant_id`` column, so a
  new table is covered as soon as its migration exists.
- :func:`scan_package` reads every ``.py`` file of a package with :mod:`ast` and rebuilds the text of each string
  expression the way Python would build it: string constants, implicit and ``+`` concatenation, f-strings,
  ``str.format``, ``str.join`` and psycopg's ``sql.SQL(...).format(...)`` / ``sql.Identifier`` / ``sql.Placeholder``
  / ``sql.Composed``. A name inside it is resolved to the string values it can hold (module constants, local
  assignments and ``+=`` in the same function, each conditional ``+=`` taken and not taken, ``for`` loops over a tuple
  of strings). Every combination is one variant, and every variant is checked.
- Each variant that names a tenant table (or that starts like a data statement and contains a part that cannot be
  resolved) is a candidate. A candidate with an unresolved part is reported as ``dynamic``; one that the PostgreSQL
  parser rejects is reported as ``unparsed``. Neither passes silently.
- A parsed candidate passes when every reference to a tenant table -- in ``FROM``, joins, ``USING``, the target of
  ``UPDATE``/``DELETE``, subqueries, CTEs and set operations -- is bound by a top-level ``AND`` conjunct of its
  statement's ``WHERE`` (or of an inner join's ``ON``) of the form ``<ref>.tenant_id = <param>`` or
  ``<ref>.tenant_id = ANY(<param>)``, or by ``<ref>.tenant_id = <other>.tenant_id`` with ``<other>`` bound (in the
  same statement or an enclosing one). ``INSERT`` into a tenant table must list ``tenant_id``. ``TRUNCATE``,
  ``COPY``, ``MERGE`` and other statements that touch a tenant table are reported as ``unsupported``.
- Everything that is cross-tenant (or not a PostgreSQL statement) by design is listed in an allowlist that maps the
  module and the **whole** normalized text (whitespace collapsed) to its reason. Matching is exact; an entry that no
  longer matches anything is reported as stale.

What it cannot see: SQL built from values that are not in the source (a query read from a file or a database), and
SQL that names no tenant table and starts with no data keyword but becomes one at run time. Both would be unusual in
this code base; the first does not exist, the second is reported when its pieces are rebuilt (``dynamic``).
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
import itertools
from pathlib import Path
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


class _Scope:
    """Where names are resolved: the enclosing function (if any) and the module."""

    def __init__(self, module: ast.Module, function: ast.AST | None, consumed: set[int], names: set[int]) -> None:
        self.module = module
        self.function = function
        self.consumed = consumed
        #: The ``Name`` nodes read while rebuilding a string (their value became part of it).
        self.names = names

    def _events(self, body_owner: ast.AST, name: str) -> list[tuple[int, str, ast.AST, bool, bool]]:
        """Assignments to ``name`` directly in ``body_owner`` (not in nested functions or classes):
        ``(line, kind, value, conditional, in_loop)``."""
        events: list[tuple[int, str, ast.AST, bool, bool]] = []

        def visit(nodes: Iterable[ast.AST], conditional: bool, loop: bool) -> None:
            for node in nodes:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                    continue
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == name:
                            events.append((node.lineno, "set", node.value, conditional, loop))
                        elif isinstance(target, (ast.Tuple, ast.List)) and any(
                            isinstance(t, ast.Name) and t.id == name for t in target.elts
                        ):
                            events.append((node.lineno, "unknown", node.value, conditional, loop))
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
                    if node.value is not None:
                        events.append((node.lineno, "set", node.value, conditional, loop))
                elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
                    kind = "add" if isinstance(node.op, ast.Add) else "unknown"
                    events.append((node.lineno, kind, node.value, conditional, loop))
                elif isinstance(node, (ast.For, ast.AsyncFor)):
                    if isinstance(node.target, ast.Name) and node.target.id == name:
                        events.append((node.lineno, "for", node.iter, conditional, loop))
                    elif _binds(node.target, name):
                        events.append((node.lineno, "unknown", node.iter, conditional, loop))
                    visit(node.body, True, True)
                    visit(node.orelse, True, loop)
                    continue
                elif isinstance(node, ast.NamedExpr) and node.target.id == name:
                    events.append((node.lineno, "unknown", node.value, conditional, loop))
                elif isinstance(node, (ast.With, ast.AsyncWith)):
                    for item in node.items:
                        if item.optional_vars is not None and _binds(item.optional_vars, name):
                            events.append((node.lineno, "unknown", item.context_expr, conditional, loop))
                    visit(node.body, conditional, loop)
                    continue
                elif isinstance(node, ast.While):
                    visit(node.body, True, True)
                    visit(node.orelse, True, loop)
                    continue
                elif isinstance(node, (ast.If, ast.Try, ast.Match)) or type(node).__name__ == "TryStar":
                    for child_field in ("body", "orelse", "finalbody", "handlers", "cases"):
                        children = getattr(node, child_field, None) or []
                        for child in children:
                            if isinstance(child, (ast.ExceptHandler, ast.match_case)):
                                if isinstance(child, ast.ExceptHandler) and child.name == name:
                                    events.append((child.lineno, "unknown", child, True, loop))
                                visit(child.body, True, loop)
                            else:
                                visit([child], True, loop)
                    continue
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        if (alias.asname or alias.name.split(".")[0]) == name:
                            events.append((node.lineno, "unknown", node, conditional, loop))
                # Expressions inside statements may hold walrus targets or comprehensions; not relevant for strings.

        visit(getattr(body_owner, "body", []), False, False)
        return events

    def resolve(self, name: str, line: int, depth: int) -> list[list[Any]]:
        owners: list[ast.AST] = []
        if self.function is not None:
            owners.append(self.function)
        owners.append(self.module)
        for owner in owners:
            if owner is self.function and isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = owner.args
                params = [*args.posonlyargs, *args.args, *args.kwonlyargs]
                if args.vararg:
                    params.append(args.vararg)
                if args.kwarg:
                    params.append(args.kwarg)
                if any(p.arg == name for p in params):
                    return [[Hole(name)]]
            events = self._events(owner, name)
            if owner is self.function:
                events = [e for e in events if e[0] <= line]
            if not events:
                continue
            return self._variants(events, name, line, depth, owner is self.module)
        return [[Hole(name)]]

    def _variants(
        self, events: list[tuple[int, str, ast.AST, bool, bool]], name: str, line: int, depth: int, module: bool
    ) -> list[list[Any]]:
        # The last unconditional assignment before the use starts the chain; earlier ones are overwritten.
        start = 0
        for index, (_line, kind, _value, conditional, _loop) in enumerate(events):
            if kind in ("set", "for", "unknown") and not conditional:
                start = index
        variants: list[list[Any]] = []
        bases = [e for e in events[start:] if e[1] in ("set", "for", "unknown")]
        for base_index, base in enumerate(events[start:], start):
            _line, kind, value, _conditional, loop = base
            if kind == "add":
                continue
            if kind == "unknown" or (loop and kind == "set" and not module):
                heads: list[list[Any]] | None = [[Hole(name)]]
            elif kind == "for":
                heads = self._iterate(value, line, depth)
            else:
                assert isinstance(value, ast.expr)
                heads = self.render(value, line, depth + 1)
            if heads is None:
                return [[Hole(name)]]
            # Following += until the next assignment: unconditional ones always apply, conditional ones may.
            chains = heads
            for later in events[base_index + 1 :]:
                if later[1] != "add":
                    if later[3]:
                        continue  # a conditional assignment may not happen: the chain goes on (its own chain too)
                    break
                assert isinstance(later[2], ast.expr)
                added = self.render(later[2], line, depth + 1)
                if added is None or later[4]:
                    added = [[Hole(f"{name} += ...")]]
                options = added if not later[3] else [*added, []]
                combined = _product([chains, options])
                if combined is None:
                    return [[Hole(name)]]
                chains = combined
            variants.extend(chains)
            if len(variants) > MAX_VARIANTS:
                return [[Hole(name)]]
        if not bases:
            return [[Hole(name)]]
        return variants

    def _iterate(self, iterable: ast.AST, line: int, depth: int) -> list[list[Any]] | None:
        if isinstance(iterable, ast.Name):
            self.names.add(id(iterable))
            events = self._events(self.module, iterable.id)
            if len(events) == 1 and events[0][1] == "set":
                iterable = events[0][2]
        if isinstance(iterable, (ast.Tuple, ast.List)):
            options: list[list[Any]] = []
            for element in iterable.elts:
                rendered = self.render(element, line, depth + 1)
                if rendered is None:
                    return None
                options.extend(rendered)
            return options
        return [[Hole(ast.unparse(iterable))]] if isinstance(iterable, ast.expr) else None

    def render(self, node: ast.expr, line: int, depth: int = 0) -> list[list[Any]] | None:
        """The possible texts of ``node`` as lists of pieces (str or :class:`Hole`); ``None``: too many variants."""
        self.consumed.add(id(node))
        if depth > 12:
            return [[Hole(ast.unparse(node))]]
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                return [[node.value]]
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
                        rendered = self.render(value.value, line, depth + 1)
                        if rendered is None:
                            return None
                        parts.append(rendered)
                else:
                    parts.append([[Hole(ast.unparse(value))]])
            return _product(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self.render(node.left, line, depth + 1)
            right = self.render(node.right, line, depth + 1)
            if left is None or right is None:
                return None
            return _product([left, right])
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            left = self.render(node.left, line, depth + 1)
            if left is None:
                return None
            return [[*variant, Hole(f"% {ast.unparse(node.right)}")] for variant in left]
        if isinstance(node, ast.IfExp):
            body = self.render(node.body, line, depth + 1)
            orelse = self.render(node.orelse, line, depth + 1)
            if body is None or orelse is None:
                return None
            return [*body, *orelse]
        if isinstance(node, ast.Name):
            self.names.add(id(node))
            return self.resolve(node.id, line, depth + 1)
        if isinstance(node, ast.Call):
            return self._render_call(node, line, depth)
        return [[Hole(ast.unparse(node))]]

    def _render_call(self, node: ast.Call, line: int, depth: int) -> list[list[Any]] | None:
        func = node.func
        # sql.SQL("..."), sql.Identifier("a", "b"), sql.Placeholder(), sql.Literal(x), sql.Composed([...])
        if _is_sql_attr(func, "SQL") and len(node.args) == 1 and not node.keywords:
            return self.render(node.args[0], line, depth + 1)
        if _is_sql_attr(func, "Identifier") and node.args and not node.keywords:
            parts: list[list[list[Any]]] = []
            for index, arg in enumerate(node.args):
                rendered = self.render(arg, line, depth + 1)
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
            return self._render_sequence(node.args[0], [[""]], line, depth)
        if isinstance(func, ast.Attribute) and func.attr == "join" and len(node.args) == 1 and not node.keywords:
            separator = self.render(func.value, line, depth + 1)
            if separator is None:
                return None
            return self._render_sequence(node.args[0], separator, line, depth)
        if isinstance(func, ast.Attribute) and func.attr == "format":
            template = self.render(func.value, line, depth + 1)
            if template is None:
                return None
            return self._render_format(template, node, line, depth)
        if isinstance(func, ast.Attribute) and func.attr == "as_string":
            return self.render(func.value, line, depth + 1)
        return [[Hole(ast.unparse(node))]]

    def _render_sequence(
        self, sequence: ast.expr, separator: list[list[Any]], line: int, depth: int
    ) -> list[list[Any]] | None:
        if not isinstance(sequence, (ast.List, ast.Tuple)) or any(isinstance(e, ast.Starred) for e in sequence.elts):
            return [[Hole(ast.unparse(sequence))]]
        parts: list[list[list[Any]]] = []
        for index, element in enumerate(sequence.elts):
            if index:
                parts.append(separator)
            rendered = self.render(element, line, depth + 1)
            if rendered is None:
                return None
            parts.append(rendered)
        return _product(parts) if parts else [[]]

    def _render_format(
        self, template: list[list[Any]], call: ast.Call, line: int, depth: int
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
                        position = int(field_name)
                        argument = call.args[position] if position < len(call.args) else None
                    else:
                        argument = keywords.get(field_name)
                    if argument is None or spec or conversion:
                        parts.append([[Hole(field_name or "{}")]])
                        continue
                    rendered = self.render(argument, line, depth + 1)
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


def _binds(target: ast.AST, name: str) -> bool:
    return any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(target))


def _is_string_root(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return _is_string_root(node.left) or _is_string_root(node.right)
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in ("format", "join", "as_string"):
            return _is_string_root(func.value)
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


def _values_used_alone(tree: ast.Module, rendered_names: set[int]) -> set[int]:
    """The ids of assigned string values (and of the elements of assigned tuples and lists) whose name is also read
    somewhere other than while rebuilding a string, in the same scope (the function that assigns it, else the
    module)."""
    assigned: dict[tuple[int, str], list[ast.AST]] = {}
    loads: dict[tuple[int, str], list[ast.Name]] = {}

    def values(node: ast.AST) -> list[ast.AST]:
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return [node, *node.elts]
        return [node]

    def visit(node: ast.AST, scope: ast.AST, local: set[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                inner = {n.id for n in ast.walk(child) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
                visit(child, child, inner)
                continue
            if isinstance(child, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and child.value is not None:
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        owner = scope if target.id in local else tree
                        assigned.setdefault((id(owner), target.id), []).extend(values(child.value))
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                owner = scope if child.id in local else tree
                loads.setdefault((id(owner), child.id), []).append(child)
            visit(child, scope, local)

    visit(tree, tree, set())
    alone: set[int] = set()
    for key, nodes in assigned.items():
        if any(id(n) not in rendered_names for n in loads.get(key, [])):
            alone.update(id(n) for n in nodes)
    return alone


def statements_of(source: str, module: str, tables: frozenset[str]) -> list[Statement]:
    """Every variant of every string expression in ``source`` that must be checked as SQL."""
    tree = ast.parse(source)
    consumed: set[int] = set()
    names: set[int] = set()
    rendered: list[tuple[ast.expr, list[list[Any]] | None]] = []
    for root, function in _roots(tree):
        scope = _Scope(tree, function, consumed, names)
        before = set(consumed)
        result = scope.render(root, getattr(root, "lineno", 0))
        consumed.discard(id(root))
        consumed |= before
        rendered.append((root, result))
    # A value that went into another string is checked there, unless its name is also used on its own.
    used_alone = _values_used_alone(tree, names)
    found: list[Statement] = []
    for root, variants in rendered:
        if id(root) in consumed and id(root) not in used_alone:
            continue  # part of another expression (a module constant or a local fragment): checked there
        line = getattr(root, "lineno", 0)
        if variants is None:
            found.append(Statement(module, line, " ".join(ast.unparse(root).split()), True))
            continue
        for pieces in variants:
            text, dynamic = _text(pieces)
            words = _words(text)
            names_table = bool(words & tables)
            if dynamic:
                if not (names_table or _builds_table_name(pieces) or _starts_like_data(text)):
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

    def _from_refs(self, items: Any, ctes: set[str]) -> tuple[list[_Ref], list[tuple[Any, list[_Ref]]]]:
        """Tenant table references of a FROM list and the join conditions that bind them (``quals``, refs)."""
        pg = _pglast("ast")
        JoinType = _pglast("enums").JoinType

        refs: list[_Ref] = []
        quals: list[tuple[Any, list[_Ref]]] = []

        def visit(item: Any) -> list[_Ref]:
            if isinstance(item, pg.RangeVar):
                if item.relname in self.tables and not (item.schemaname is None and item.relname in ctes):
                    ref = _Ref(item.alias.aliasname if item.alias else item.relname, item.relname)
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
        return refs, quals

    def _bind(self, refs: list[_Ref], pools: list[tuple[Any, list[_Ref]]], outer: list[_Ref]) -> None:
        """Mark refs bound by ``tenant_id = param``/``= ANY(param)`` or by equality with a bound ref."""
        pg = _pglast("ast")
        A_Expr_Kind = _pglast("enums").A_Expr_Kind

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
                        partner = [r for r in [*refs, *outer] if r.name == other_qualifier[-1] and r is not ref]
                        if len(partner) == 1 and (partner[0].bound or any(partner[0] is o for o in outer)):
                            ref.bound = changed = True

    def check(self, node: Any, outer: list[_Ref], ctes: set[str]) -> None:
        pg = _pglast("ast")

        own: list[_Ref] = []
        if isinstance(node, (pg.SelectStmt, pg.UpdateStmt, pg.DeleteStmt, pg.InsertStmt)):
            with_clause = getattr(node, "withClause", None)
            if with_clause is not None:
                for cte in with_clause.ctes or ():
                    if cte.ctename in self.tables:
                        self.problems.append(f"CTE named like the tenant table {cte.ctename}")
                    ctes = ctes | {cte.ctename}
        if isinstance(node, pg.SelectStmt):
            own, quals = self._from_refs(node.fromClause, ctes)
            self._bind(own, [(node.whereClause, own), *quals], outer)
        elif isinstance(node, (pg.UpdateStmt, pg.DeleteStmt)):
            target = [node.relation]
            extra = node.fromClause if isinstance(node, pg.UpdateStmt) else node.usingClause
            own, quals = self._from_refs([*target, *(extra or ())], ctes)
            self._bind(own, [(node.whereClause, own), *quals], outer)
        elif isinstance(node, pg.InsertStmt):
            table = node.relation.relname
            if table in self.tables:
                columns = [c.name for c in node.cols or ()]
                if "tenant_id" not in columns:
                    self.problems.append(f"{table}: INSERT without tenant_id in the column list")
        for ref in own:
            if not ref.bound:
                self.problems.append(
                    f"{ref.table}"
                    + (f" (as {ref.name})" if ref.name != ref.table else "")
                    + " is not bound to one tenant by a top-level `tenant_id = <param>` conjunct"
                )
        for child in _children(node):
            self.check(child, [*outer, *own], ctes)


def check_sql(text: str, tables: frozenset[str]) -> list[str]:
    """Why ``text`` (one or more PostgreSQL statements with psycopg placeholders) may read or change another tenant's
    rows; empty when every reference to a tenant table is bound to one tenant. Raises ``pglast.parser.ParseError``."""
    parse_sql = _pglast("").parse_sql

    problems: list[str] = []
    for raw in parse_sql(to_postgres_params(text)):
        stmt = raw.stmt
        kind = type(stmt).__name__
        if kind in _DATA_STATEMENTS or kind == "ExplainStmt":
            checker = _Checker(tables)
            checker.check(stmt, [], set())
            problems.extend(checker.problems)
            continue
        touched = sorted({n.relname for n in _walk(stmt) if type(n).__name__ == "RangeVar" and n.relname in tables})
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
                report.findings.append(
                    Finding(module, statement.line, "dynamic", statement.text, "SQL with a part that is not known")
                )
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
