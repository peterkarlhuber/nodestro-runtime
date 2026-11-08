"""No statement that writes goes through the retrying ``PostgresDatabase._query`` (JQ-3, RT2-03).

``_query`` repeats a statement on another connection when its connection is lost. A write may already have been
applied when that happens, so writes go through ``_write`` or ``_write_returning``, which never repeat. This guard
reads the runtime's source (AST) and resolves the SQL text of every ``._query(...)`` call: string literals,
f-strings (their substitutions count as unknown text), ``+`` concatenations, and names assigned in the same function
or at module level. A call whose text writes (``INSERT``, ``UPDATE ... SET``, ``DELETE``, ``MERGE``, DDL and other
utility statements, or a function with a side effect such as ``nextval`` or an advisory lock), or whose text cannot be
resolved, must be in :data:`ALLOWED` with the reason it is safe. The reconciliation reads passed to
``_write_returning(..., if_lost=...)`` must be reads too.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
import re

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "src" / "nodestro_runtime"

#: ``(file, function)`` -> why its ``_query`` call may write or need not be resolved. Keep this short: a new entry
#: needs a reason that a repeat after a lost reply has the same effect and the same answer as a single run.
ALLOWED: dict[tuple[str, str], str] = {
    ("jobs.py", "JobQueue.heartbeat"): "renews the lease again: same row, same answer (idempotent)",
    ("jobs.py", "JobQueue.report"): "merges the same keys into the payload again and renews the lease (idempotent)",
    ("identity_store.py", "IdentityStore.set_tenant_status"): "sets the same status again (idempotent)",
    ("identity_store.py", "IdentityStore.login"): (
        "the repeat meets the row the first attempt inserted (ON CONFLICT DO UPDATE) and returns its id"
    ),
    ("storage_postgres.py", "PostgresStore.lock"): (
        "pg_advisory_xact_lock only inside a transaction (checked), where _query uses its connection and never retries"
    ),
    ("storage_postgres.py", "_Reads.execute"): (
        "DatabaseSource's read mode: Orkexa's PostgreSQL backends run only reads in it; writes use _Writes"
    ),
    ("storage_postgres.py", "PostgresDatabase._write_returning"): (
        "the reconciliation read of if_lost; every if_lost passed in the source is checked to be a read"
    ),
}

_SPACE = re.compile(r"\s+")
#: SQL string literals are data (a privilege name such as 'INSERT', a status), never a statement.
_LITERAL = re.compile(r"'(?:[^']|'')*'")
#: Statements and functions that change something. The text is upper-cased with single spaces first; ``FOR UPDATE``,
#: ``FOR NO KEY UPDATE``, ``DO UPDATE`` (inside an INSERT) and ``ON DELETE`` are row locks or clauses, not writes.
_WRITES = re.compile(
    r"\bINSERT\b|(?<!FOR )(?<!KEY )(?<!DO )\bUPDATE\b|(?<!ON )\bDELETE\b|\bMERGE\b|\bTRUNCATE\b|\bUPSERT\b"
    r"|\b(?:CREATE|ALTER|DROP|GRANT|REVOKE|COPY|VACUUM|REINDEX|CLUSTER|REFRESH|COMMENT ON|SECURITY LABEL|LOCK TABLE"
    r"|CALL|DISCARD|NOTIFY|LISTEN|PREPARE|EXECUTE|DEALLOCATE|IMPORT FOREIGN SCHEMA|REASSIGN|SET ROLE|RESET)\b"
    r"|\b(?:NEXTVAL|SETVAL|SET_CONFIG|PG_NOTIFY|PG_TERMINATE_BACKEND|PG_CANCEL_BACKEND|PG_RELOAD_CONF"
    r"|PG_(?:TRY_)?ADVISORY_(?:XACT_)?LOCK(?:_SHARED)?|PG_ADVISORY_UNLOCK(?:_SHARED|_ALL)?|LO_\w+)\s*\("
)
#: Stands for text an f-string substitutes (column lists, conditions): it is never a keyword itself.
_UNKNOWN = " ? "


def writes(sql: str) -> bool:
    """Whether the SQL text runs a statement or function that changes something."""
    return _WRITES.search(_SPACE.sub(" ", _LITERAL.sub("''", sql).upper())) is not None


@dataclass(frozen=True)
class Call:
    file: str
    function: str
    line: int
    texts: tuple[str, ...] | None  # every SQL text the call may run; None: not resolved


class _Scope:
    """The string assignments of a module and of each function, for resolving a name to its SQL text."""

    def __init__(self, tree: ast.Module) -> None:
        self.module = self._assignments(tree.body)

    @staticmethod
    def _assignments(body: list[ast.stmt] | ast.AST) -> dict[str, list[ast.expr]]:
        found: dict[str, list[ast.expr]] = {}
        nodes = ast.walk(body) if isinstance(body, ast.AST) else iter(body)
        for node in nodes:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        found.setdefault(target.id, []).append(node.value)
            elif (
                isinstance(node, ast.AnnAssign | ast.AugAssign)
                and isinstance(node.target, ast.Name)
                and node.value is not None
            ):
                found.setdefault(node.target.id, []).append(node.value)
        return found

    def texts(self, node: ast.expr, local: dict[str, list[ast.expr]], depth: int = 0) -> list[str] | None:
        """Every text ``node`` may be (a name's assignments and ``+=`` parts are all included); None: unknown."""
        if depth > 8:
            return None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, ast.JoinedStr):
            return [
                "".join(
                    part.value if isinstance(part, ast.Constant) and isinstance(part.value, str) else _UNKNOWN
                    for part in node.values
                )
            ]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self.texts(node.left, local, depth + 1), self.texts(node.right, local, depth + 1)
            if left is None or right is None:
                return None
            return [a + b for a in left for b in right]
        if isinstance(node, ast.Name):
            values = local.get(node.id) or self.module.get(node.id)
            if not values:
                return None
            parts: list[str] = []
            for value in values:
                found = self.texts(value, local, depth + 1)
                if found is None:
                    return None
                parts.extend(found)
            return [" ".join(parts)]  # every fragment together: a keyword in any of them counts
        return None


def calls(path: Path, root: Path = SOURCE) -> tuple[list[Call], list[Call]]:
    """The ``._query`` calls of a file, and the ``if_lost`` reads of its ``._write_returning`` calls."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    scope = _Scope(tree)
    queries: list[Call] = []
    reconciliations: list[Call] = []
    name = path.relative_to(root).as_posix()

    def visit(node: ast.AST, qualname: list[str], local: dict[str, list[ast.expr]]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, [*qualname, child.name], local)
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                visit(child, [*qualname, child.name], scope._assignments(child))
            else:
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                    function = ".".join(qualname) or "<module>"
                    if child.func.attr == "_query":
                        first = child.args[0] if child.args and not isinstance(child.args[0], ast.Starred) else None
                        found = None if first is None else scope.texts(first, local)
                        queries.append(Call(name, function, child.lineno, None if found is None else tuple(found)))
                    elif child.func.attr == "_write_returning":
                        for keyword in child.keywords:
                            if keyword.arg != "if_lost":
                                continue
                            value = keyword.value
                            first = value.elts[0] if isinstance(value, ast.Tuple) and value.elts else None
                            found = None if first is None else scope.texts(first, local)
                            reconciliations.append(
                                Call(name, function, child.lineno, None if found is None else tuple(found))
                            )
                visit(child, qualname, local)

    visit(tree, [], {})
    return queries, reconciliations


def all_calls() -> tuple[list[Call], list[Call]]:
    queries: list[Call] = []
    reconciliations: list[Call] = []
    for path in sorted(SOURCE.rglob("*.py")):
        found, reads = calls(path)
        queries.extend(found)
        reconciliations.extend(reads)
    return queries, reconciliations


def test_no_write_goes_through_the_retrying_query() -> None:
    queries, _ = all_calls()
    assert len(queries) > 50, "the scan found too few _query calls; is SOURCE right?"
    offending = [
        f"{call.file}:{call.line} in {call.function}: "
        + ("SQL text not resolved" if call.texts is None else "writes: " + _SPACE.sub(" ", call.texts[0])[:100])
        for call in queries
        if (call.texts is None or any(writes(text) for text in call.texts))
        and (call.file, call.function) not in ALLOWED
    ]
    assert not offending, (
        "These statements go through PostgresDatabase._query, which repeats them after a lost connection (RT2-03)."
        " Use _write or _write_returning, or add the call to ALLOWED with the reason a repeat is safe:\n"
        + "\n".join(offending)
    )


def test_every_allowed_entry_is_still_needed() -> None:
    queries, _ = all_calls()
    needed = {
        (call.file, call.function) for call in queries if call.texts is None or any(writes(text) for text in call.texts)
    }
    assert sorted(set(ALLOWED) - needed) == []


def test_every_reconciliation_after_a_lost_write_is_a_read() -> None:
    _, reconciliations = all_calls()
    assert {call.function for call in reconciliations} >= {
        "JobQueue.enqueue",
        "JobQueue.claim",
        "IdentityStore.create_tenant",
    }
    bad = [call for call in reconciliations if call.texts is None or any(writes(text) for text in call.texts)]
    assert bad == []


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO nodestro_jobs (a) VALUES (%s) ON CONFLICT DO NOTHING RETURNING id",
        "UPDATE nodestro_jobs SET status = 'running' WHERE id = (SELECT id FROM t FOR UPDATE SKIP LOCKED) RETURNING id",
        "update  nodestro_tenants\n set status = %s",
        "DELETE FROM nodestro_login_states WHERE state_hash = %s RETURNING nonce",
        "WITH moved AS (DELETE FROM a RETURNING *) SELECT * FROM moved",
        "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET x = 1",
        "TRUNCATE nodestro_jobs",
        "CREATE TABLE x (id int)",
        "SELECT nextval('s')",
        "SELECT pg_advisory_lock(1)",
        "SELECT pg_try_advisory_xact_lock(1, 2)",
        "SELECT set_config('search_path', %s, false)",
    ],
)
def test_the_guard_recognizes_writes(sql: str) -> None:
    assert writes(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM nodestro_jobs WHERE status = 'queued' FOR UPDATE SKIP LOCKED",
        "SELECT id FROM t FOR NO KEY UPDATE",
        "SELECT created_at, updated_at, deleted FROM nodestro_documents",
        "SELECT to_regclass('nodestro_schema_migrations') IS NOT NULL",
        "SELECT pg_has_role(%s, c.relowner, 'MEMBER') FROM pg_class c",
        "SELECT has_table_privilege(%s, c.oid, 'INSERT, UPDATE, DELETE'), 'it''s CREATE' FROM pg_class c",
    ],
)
def test_the_guard_lets_reads_pass(sql: str) -> None:
    assert not writes(sql)


def test_the_guard_resolves_names_concatenations_and_f_strings(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    source.write_text(
        "COLUMNS = 'id'\n"
        "WRITE = 'UPDATE t SET a = 1'\n"
        "class Store:\n"
        "    def read(self):\n"
        "        query = f'SELECT {COLUMNS} FROM t'\n"
        "        query += ' ORDER BY id'\n"
        "        return self.db._query(query + ' LIMIT 1')\n"
        "    def write(self):\n"
        "        return self.db._query(WRITE)\n"
        "    def hidden(self):\n"
        "        query = 'SELECT 1'\n"
        "        query += ' ; DELETE FROM t'\n"
        "        return self.db._query(query)\n"
        "    def unknown(self, query):\n"
        "        return self.db._query(query)\n"
        "    def lost(self):\n"
        "        return self.db._write_returning('DELETE FROM t RETURNING id', if_lost=('SELECT 1', ()))\n",
        encoding="utf-8",
    )
    queries, reconciliations = calls(source, tmp_path)
    by_function = {call.function: call for call in queries}
    assert not any(writes(text) for text in by_function["Store.read"].texts or ("DELETE",))
    assert any(writes(text) for text in by_function["Store.write"].texts or ())
    assert any(writes(text) for text in by_function["Store.hidden"].texts or ())
    assert by_function["Store.unknown"].texts is None
    assert [(call.function, call.texts) for call in reconciliations] == [("Store.lost", ("SELECT 1",))]
