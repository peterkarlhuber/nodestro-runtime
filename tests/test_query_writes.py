"""No statement that writes goes through the retrying ``PostgresDatabase._query`` (JQ-3, RT2-03).

``_query`` repeats a statement on another connection when its connection is lost. A write may already have been
applied when that happens, so writes go through ``_write`` or ``_write_returning``, which never repeat. This guard
reads the runtime's source (AST) and resolves the SQL text of every ``._query(...)`` call: string literals,
f-strings (a substitution of a name is resolved; any other substitution is unknown text, shown as ``{expression}``),
``+`` concatenations, and names assigned in the same function or at module level.

- A call whose text writes (``INSERT``, ``UPDATE ... SET``, ``DELETE``, ``MERGE``, ``SELECT ... INTO``, ``SET``, ``DO``,
  DDL and other utility statements, or a function with a side effect such as ``nextval`` or an advisory lock) must
  be in :data:`ALLOWED_WRITES`, keyed by its file and its **whole normalized statement**, with the reason a repeat is
  safe. A changed or added statement needs its own entry.
- A call whose text cannot be resolved must be in :data:`ALLOWED_UNRESOLVED`, keyed by file and function.
- ``_query`` may only be called directly: an alias (``q = db._query``), ``getattr(db, "_query")`` or any other use of
  the attribute outside ``storage_postgres.py`` fails.
- The reconciliation reads passed to ``_write_returning(..., if_lost=...)`` must be reads.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
import re

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "src" / "nodestro_runtime"

#: ``(file, normalized statement)`` -> why a repeat after a lost reply has the same effect and the same answer.
ALLOWED_WRITES: dict[tuple[str, str], str] = {
    (
        "jobs.py",
        "UPDATE nodestro_jobs SET lease_until = clock_timestamp() + %s WHERE id = %s AND worker = %s AND status ="
        " 'running' RETURNING id, tenant_id, kind, ref, status, payload, attempts, worker, lease_until,"
        " cancel_requested, error, created_at, started_at, finished_at",
    ): "heartbeat: renews the lease of the worker's own job again; same row, same answer",
    (
        "jobs.py",
        "UPDATE nodestro_jobs SET payload = payload || %s::jsonb, lease_until = clock_timestamp() + %s WHERE tenant_id"
        " = %s AND id = %s AND worker = %s AND status = 'running' RETURNING id, tenant_id, kind, ref, status, payload,"
        " attempts, worker, lease_until, cancel_requested, error, created_at, started_at, finished_at",
    ): "report: merges the same keys into the worker's own job again and renews its lease",
    (
        "identity_store.py",
        "INSERT INTO nodestro_users (id, issuer, subject, email, name, last_login_at) VALUES (%s, %s, %s, %s, %s,"
        " clock_timestamp()) ON CONFLICT (issuer, subject) DO UPDATE SET email = excluded.email, name ="
        " excluded.name, last_login_at = excluded.last_login_at RETURNING id",
    ): (
        "login: the repeat meets the row the first attempt inserted and returns its id; at most the e-mail or name"
        " of a concurrent login of the same identity is written once more"
    ),
    ("storage_postgres.py", "SELECT pg_advisory_xact_lock(%s, hashtext(%s))"): (
        "PostgresStore.lock: only inside a transaction (checked), where _query uses its connection and never retries"
    ),
}

#: ``(file, function)`` -> why the SQL text of its ``_query`` call cannot be resolved here, and why it is a read.
ALLOWED_UNRESOLVED: dict[tuple[str, str], str] = {
    ("storage_postgres.py", "_Reads.execute"): (
        "DatabaseSource's read mode: Orkexa's PostgreSQL backends run only reads in it; writes use _Writes"
    ),
    ("storage_postgres.py", "PostgresDatabase._write_returning"): (
        "the reconciliation read of if_lost; every if_lost passed in the source is checked to be a read"
    ),
    ("jobs.py", "JobQueue.claim"): (
        "the adoption read and the if_lost read of JobQueue._unheld (a SELECT, checked by"
        " test_the_adoption_read_of_a_claim_is_a_read)"
    ),
}

_SPACE = re.compile(r"\s+")
#: SQL string literals are data (a privilege name such as 'INSERT', a status), never a statement.
_LITERAL = re.compile(r"'(?:[^']|'')*'")
#: Statements and functions that change something. The text is upper-cased with single spaces first; ``FOR UPDATE``,
#: ``FOR NO KEY UPDATE``, ``DO UPDATE`` (inside an INSERT) and ``ON DELETE`` are row locks or clauses, not writes.
_WRITES = re.compile(
    r"\bINSERT\b|(?<!FOR )(?<!KEY )(?<!DO )\bUPDATE\b|(?<!ON )\bDELETE\b|\bMERGE\b|\bTRUNCATE\b|\bUPSERT\b"
    r"|\bSELECT\b[^;]*?(?<!INSERT )(?<!MERGE )\bINTO\b|(?:^|;) ?SET\b|\bDO ?\$"
    r"|\b(?:CREATE|ALTER|DROP|GRANT|REVOKE|COPY|VACUUM|REINDEX|CLUSTER|REFRESH|COMMENT ON|SECURITY LABEL|LOCK TABLE"
    r"|CALL|DISCARD|NOTIFY|LISTEN|PREPARE|EXECUTE|DEALLOCATE|IMPORT FOREIGN SCHEMA|REASSIGN|SET ROLE|RESET)\b"
    r"|\b(?:NEXTVAL|SETVAL|SET_CONFIG|PG_NOTIFY|PG_TERMINATE_BACKEND|PG_CANCEL_BACKEND|PG_RELOAD_CONF"
    r"|PG_(?:TRY_)?ADVISORY_(?:XACT_)?LOCK(?:_SHARED)?|PG_ADVISORY_UNLOCK(?:_SHARED|_ALL)?|LO_\w+)\s*\("
)


def normalized(sql: str) -> str:
    return _SPACE.sub(" ", sql).strip()


def writes(sql: str) -> bool:
    """Whether the SQL text runs a statement or function that changes something."""
    return _WRITES.search(normalized(_LITERAL.sub("''", sql)).upper()) is not None


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
            variants = [""]
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    variants = [text + part.value for text in variants]
                    continue
                assert isinstance(part, ast.FormattedValue)
                resolved = self.texts(part.value, local, depth + 1) if isinstance(part.value, ast.Name) else None
                options = resolved if resolved is not None else ["{" + ast.unparse(part.value) + "}"]
                variants = [text + option for text in variants for option in options]
            return variants
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


def calls(path: Path, root: Path = SOURCE) -> tuple[list[Call], list[Call], list[str]]:
    """The ``._query`` calls of a file, the ``if_lost`` reads of its ``._write_returning`` calls, and every other use
    of ``_query`` (an alias, ``getattr``)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    scope = _Scope(tree)
    queries: list[Call] = []
    reconciliations: list[Call] = []
    other: list[str] = []
    name = path.relative_to(root).as_posix()
    direct: set[int] = set()

    def visit(node: ast.AST, qualname: list[str], local: dict[str, list[ast.expr]]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, [*qualname, child.name], local)
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                visit(child, [*qualname, child.name], scope._assignments(child))
            else:
                function = ".".join(qualname) or "<module>"
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                    if child.func.attr == "_query":
                        direct.add(id(child.func))
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
                if isinstance(child, ast.Attribute) and child.attr == "_query" and id(child) not in direct:
                    other.append(f"{name}:{child.lineno} in {function}: _query used without calling it directly")
                if (
                    isinstance(child, ast.Constant)
                    and child.value == "_query"
                    and not isinstance(node, ast.Expr)  # a docstring line is not a use
                ):
                    other.append(f"{name}:{child.lineno} in {function}: the name '_query' as a string (getattr?)")
                visit(child, qualname, local)

    visit(tree, [], {})
    return queries, reconciliations, other


def all_calls() -> tuple[list[Call], list[Call], list[str]]:
    queries: list[Call] = []
    reconciliations: list[Call] = []
    other: list[str] = []
    for path in sorted(SOURCE.rglob("*.py")):
        found, reads, uses = calls(path)
        queries.extend(found)
        reconciliations.extend(reads)
        if path.name != "storage_postgres.py":
            other.extend(uses)
    return queries, reconciliations, other


def _offending(queries: list[Call]) -> tuple[list[str], set[tuple[str, str]], set[tuple[str, str]]]:
    offending: list[str] = []
    used_writes: set[tuple[str, str]] = set()
    used_unresolved: set[tuple[str, str]] = set()
    for call in queries:
        if call.texts is None:
            if (call.file, call.function) in ALLOWED_UNRESOLVED:
                used_unresolved.add((call.file, call.function))
            else:
                offending.append(f"{call.file}:{call.line} in {call.function}: SQL text not resolved")
            continue
        for text in call.texts:
            if not writes(text):
                continue
            key = (call.file, normalized(text))
            if key in ALLOWED_WRITES:
                used_writes.add(key)
            else:
                offending.append(f"{call.file}:{call.line} in {call.function}: writes: {normalized(text)}")
    return offending, used_writes, used_unresolved


def test_no_write_goes_through_the_retrying_query() -> None:
    queries, _, other = all_calls()
    assert len(queries) > 50, "the scan found too few _query calls; is SOURCE right?"
    offending, _, _ = _offending(queries)
    assert not offending + other, (
        "These statements go through PostgresDatabase._query, which repeats them after a lost connection (RT2-03)."
        " Use _write or _write_returning, or add the statement to ALLOWED_WRITES with the reason a repeat is safe:\n"
        + "\n".join(offending + other)
    )


def test_every_allowed_entry_is_still_needed() -> None:
    queries, _, _ = all_calls()
    _, used_writes, used_unresolved = _offending(queries)
    assert sorted(set(ALLOWED_WRITES) - used_writes) == []
    assert sorted(set(ALLOWED_UNRESOLVED) - used_unresolved) == []


def test_every_reconciliation_after_a_lost_write_is_a_read() -> None:
    _, reconciliations, _ = all_calls()
    assert {call.function for call in reconciliations} >= {
        "JobQueue.enqueue",
        "JobQueue.claim",
        "IdentityStore.create_tenant",
        "IdentityStore.set_tenant_status",
    }
    bad = [
        call
        for call in reconciliations
        if (call.texts is None and (call.file, call.function) not in ALLOWED_UNRESOLVED)
        or any(writes(text) for text in call.texts or ())
    ]
    assert bad == []


def test_the_adoption_read_of_a_claim_is_a_read() -> None:
    from nodestro_runtime.jobs import JobQueue

    query, _params = JobQueue._unheld("w", ["run"], [])
    assert query.startswith("SELECT ") and not writes(query)


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
        "SELECT * INTO copy_of_t FROM t",
        "SET statement_timeout = 0",
        "set local lock_timeout = '1s'",
        "SELECT 1; SET search_path TO x",
        "DO $$ BEGIN PERFORM 1; END $$",
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
        "SELECT created_at, updated_at, deleted, offset_into FROM nodestro_documents",
        "SELECT to_regclass('nodestro_schema_migrations') IS NOT NULL",
        "SELECT pg_has_role(%s, c.relowner, 'MEMBER') FROM pg_class c",
        "SELECT has_table_privilege(%s, c.oid, 'INSERT, UPDATE, DELETE'), 'it''s CREATE' FROM pg_class c",
        "SELECT a FROM t WHERE b = 'SET x'",
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
        "    def read(self, where):\n"
        "        query = f'SELECT {COLUMNS} FROM t WHERE {where}'\n"
        "        query += ' ORDER BY id'\n"
        "        return self.db._query(query + ' LIMIT 1')\n"
        "    def write(self):\n"
        "        return self.db._query(WRITE)\n"
        "    def hidden_in_an_f_string(self):\n"
        "        return self.db._query(f'{WRITE}')\n"
        "    def hidden(self):\n"
        "        query = 'SELECT 1'\n"
        "        query += ' ; DELETE FROM t'\n"
        "        return self.db._query(query)\n"
        "    def unknown(self, query):\n"
        "        return self.db._query(query)\n"
        "    def alias(self):\n"
        "        run = self.db._query\n"
        "        return run('DELETE FROM t')\n"
        "    def by_name(self):\n"
        "        return getattr(self.db, '_query')('DELETE FROM t')\n"
        "    def lost(self):\n"
        "        return self.db._write_returning('DELETE FROM t RETURNING id', if_lost=('SELECT 1', ()))\n",
        encoding="utf-8",
    )
    queries, reconciliations, other = calls(source, tmp_path)
    by_function = {call.function: call for call in queries}
    assert [normalized(text) for text in by_function["Store.read"].texts or ()] == [
        "SELECT id FROM t WHERE {where} ORDER BY id LIMIT 1"
    ]
    assert not writes(by_function["Store.read"].texts[0])
    assert any(writes(text) for text in by_function["Store.write"].texts or ())
    assert any(writes(text) for text in by_function["Store.hidden_in_an_f_string"].texts or ())
    assert any(writes(text) for text in by_function["Store.hidden"].texts or ())
    assert by_function["Store.unknown"].texts is None
    assert [line.split(" in ")[1].split(":")[0] for line in other] == ["Store.alias", "Store.by_name"]
    assert [(call.function, call.texts) for call in reconciliations] == [("Store.lost", ("SELECT 1",))]
