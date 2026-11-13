"""PostgreSQL for the tests: ``NODESTRO_TEST_POSTGRES`` names a database (a libpq connection string).

Each test gets its own schema, dropped afterwards. Without the variable the PostgreSQL cases are skipped, unless
``NODESTRO_REQUIRE_POSTGRES=1`` (CI), which turns the missing database into a failure.

``NODESTRO_TEST_RUNTIME_ROLE=1`` runs every PostgreSQL case as a production server does: the role of
``NODESTRO_TEST_POSTGRES`` is the **owner** (it runs every migration and owns the tables), and every
:class:`~nodestro_runtime.storage_postgres.PostgresDatabase` the tests open on it connects as the restricted runtime
role ``nodestro_test_runtime`` instead (no superuser, no ``BYPASSRLS``, owns nothing, privileges from
:func:`~nodestro_runtime.storage_postgres.grant_runtime_role` only). So Nodestro's and Axentra's code paths are proven
to work without DDL rights and under Axentra's row-level security (see :func:`install_runtime_role_mode`).

The decision whether a PostgreSQL case runs, skips or fails comes from ``tabrion.testing`` (TABRION MIG-NOD-1): the
plugin is active in ``conftest.py`` (``pytest_plugins``), :func:`configure_tabrion` points it at the variables above,
and :func:`require_postgres` resolves its session fixture ``tabrion_postgres_url``. The helpers keep their names and
their function form, so a test module calls them from any fixture or test, whatever its scope.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
import gzip
import importlib.util
import os
from pathlib import Path
import socket
import threading
from typing import Any
import uuid

import pytest

#: The databases this pytest-xdist worker created for itself: ``(original DSN, database)`` per variable.
XDIST_DATABASES: dict[str, tuple[str, str]] = {}


def _own_database_per_xdist_worker() -> None:
    """Under pytest-xdist every worker process gets its own PostgreSQL database (CI-FAST AP6).

    Each test migrates into its own schema, but some state belongs to the whole database: session advisory locks
    (the scheduler leader) and the pgvector extension, which the upgrade and drift tests install and remove while
    other tests use it. Workers sharing one database would change that state under each other's tests; a worker's
    own database keeps them apart, exactly as in a serial run. ``NODESTRO_TEST_POSTGRES`` and
    ``NODESTRO_TEST_PGVECTOR`` are rewritten when this module is first imported (by ``conftest.py``, before any test
    module reads them), so the tests and the child processes they start use the worker's database; ``conftest.py``
    drops it when the worker ends (:func:`drop_xdist_databases`). Serial runs are unchanged.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    # A child process a test starts inherits the worker's variables (already rewritten): it uses the same database.
    if not worker or os.environ.get("NODESTRO_TEST_XDIST_DATABASE") == worker:
        return
    created: dict[str, str] = {}  # original DSN -> the worker's DSN (both variables usually name one database)
    for variable in ("NODESTRO_TEST_POSTGRES", "NODESTRO_TEST_PGVECTOR"):
        dsn = os.environ.get(variable)
        if not dsn:
            continue
        if dsn not in created:
            import psycopg
            from psycopg import sql
            from psycopg.conninfo import conninfo_to_dict, make_conninfo

            name = f"{conninfo_to_dict(dsn).get('dbname') or 'postgres'}_xdist_{worker}"
            with psycopg.connect(dsn, autocommit=True) as admin:
                admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
                admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            created[dsn] = make_conninfo(dsn, dbname=name)
            XDIST_DATABASES[variable] = (dsn, name)
        os.environ[variable] = created[dsn]
    os.environ["NODESTRO_TEST_XDIST_DATABASE"] = worker


def drop_xdist_databases() -> None:
    """Drop the databases :func:`_own_database_per_xdist_worker` created (called when the worker ends)."""
    import psycopg
    from psycopg import sql

    for dsn, name in dict.fromkeys(XDIST_DATABASES.values()):
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


_own_database_per_xdist_worker()

DSN = os.environ.get("NODESTRO_TEST_POSTGRES", "")
REQUIRED = os.environ.get("NODESTRO_REQUIRE_POSTGRES") == "1"
RUNTIME_ROLE_MODE = os.environ.get("NODESTRO_TEST_RUNTIME_ROLE") == "1"
RUNTIME_ROLE = "nodestro_test_runtime"
RUNTIME_PASSWORD = "nodestro-test-runtime-pw"
#: The legacy PostgreSQL dumps FX-PG-0 and FX-PG-0v (MUP-ONA A0, ``tests/fixtures/legacy/README.md``).
LEGACY = Path(__file__).resolve().parent / "fixtures" / "legacy"


#: Whether ``tabrion.testing`` is installed (the ``dev`` extra). CI's platform jobs install without it and run no
#: PostgreSQL case; there :func:`require_postgres` skips (fails under ``NODESTRO_REQUIRE_POSTGRES=1``) by itself.
TABRION_TESTING = (
    importlib.util.find_spec("tabrion") is not None and importlib.util.find_spec("tabrion.testing") is not None
)

#: The pytest session's fixture request (:func:`tabrion_session`), through which :func:`require_postgres` resolves
#: ``tabrion.testing``'s session fixtures from plain functions.
_SESSION: pytest.FixtureRequest | None = None


def configure_tabrion() -> None:
    """Point ``tabrion.testing`` at Nodestro's variables (called by ``conftest.py`` in ``pytest_configure``).

    ``NODESTRO_TEST_POSTGRES``, ``NODESTRO_REQUIRE_POSTGRES`` and ``NODESTRO_TEST_RUNTIME_ROLE`` keep their names
    and meaning, so the CI jobs are unchanged. Anything the plugin's own fixtures create starts with
    ``nodestro_test_``, like the schemas of :func:`new_schema`. Imported here, not at module level, so the plugin
    module is first imported by pytest itself (``pytest_plugins``) and its assertions are rewritten.
    """
    if not TABRION_TESTING:
        return
    from tabrion.testing import configure

    configure(env_prefix="NODESTRO", schema_prefix="nodestro_test_")


@pytest.fixture(scope="session", autouse=True)
def tabrion_session(request: pytest.FixtureRequest) -> Iterator[None]:
    """Keep the session's fixture request for :func:`require_postgres` (resolves nothing by itself)."""
    global _SESSION
    _SESSION = request
    try:
        yield
    finally:
        _SESSION = None


def require_postgres() -> str:
    """The test database, or skip (fail when PostgreSQL is required).

    Through ``tabrion.testing``'s session fixture ``tabrion_postgres_url``: without ``NODESTRO_TEST_POSTGRES`` the
    calling test skips, under ``NODESTRO_REQUIRE_POSTGRES=1`` it fails; the outcome is resolved once per session.
    """
    if not TABRION_TESTING:
        if os.environ.get("NODESTRO_REQUIRE_POSTGRES") == "1":
            pytest.fail("NODESTRO_REQUIRE_POSTGRES=1 but tabrion[testing] (the dev extra) is not installed")
        pytest.skip("PostgreSQL tests need the dev extra (tabrion[testing])")
    if _SESSION is None:
        raise RuntimeError("require_postgres() runs only inside a pytest session (conftest.py loads tabrion_session)")
    return str(_SESSION.getfixturevalue("tabrion_postgres_url"))


def new_schema() -> str:
    return f"nodestro_test_{uuid.uuid4().hex[:16]}"


def drop_schema(schema: str) -> None:
    import psycopg
    from psycopg import sql

    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def runtime_dsn() -> str:
    """``DSN`` with the runtime role's credentials."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    parts = conninfo_to_dict(DSN)
    parts.update(user=RUNTIME_ROLE, password=RUNTIME_PASSWORD)
    return make_conninfo("", **parts)


def install_runtime_role_mode() -> None:
    """``NODESTRO_TEST_RUNTIME_ROLE=1``: databases opened on ``DSN`` connect as the runtime role; their ``migrate``
    runs as the owner (``DSN``) and then grants the runtime role its privileges, as ``nodestro-server migrate`` and
    ``db grant-runtime-role`` do on a server."""
    import psycopg
    from psycopg import sql

    from nodestro_runtime import storage_postgres

    with psycopg.connect(DSN, autocommit=True) as admin:
        if admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (RUNTIME_ROLE,)).fetchone() is None:
            # Roles belong to the whole server: pytest-xdist workers create it at the same time, and only one wins.
            with suppress(psycopg.errors.DuplicateObject, psycopg.errors.UniqueViolation):
                admin.execute(
                    sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD {}").format(
                        sql.Identifier(RUNTIME_ROLE), sql.Literal(RUNTIME_PASSWORD)
                    )
                )
    cls = storage_postgres.PostgresDatabase
    original_init, original_migrate, original_close = cls.__init__, cls.migrate, cls.close
    runtime = runtime_dsn()
    granting = threading.Lock()  # concurrent GRANTs on one schema fail with "tuple concurrently updated"

    def init(self, conninfo, *args, **kwargs):  # type: ignore[no-untyped-def]
        if conninfo == DSN:
            self._test_owner_dsn = DSN
            conninfo = runtime
        original_init(self, conninfo, *args, **kwargs)

    def migrate(self, **kwargs):  # type: ignore[no-untyped-def]
        if getattr(self, "_test_owner_dsn", None) is None:
            return original_migrate(self, **kwargs)
        if self.in_transaction:
            raise RuntimeError("migrate() cannot run inside a transaction")
        version = original_migrate(owner_of(self), **kwargs)
        with granting:
            report = storage_postgres.grant_runtime_role(owner_of(self), RUNTIME_ROLE)
        assert report["restricted"], report
        return version

    def owner(self):  # type: ignore[no-untyped-def]
        if getattr(self, "_test_owner_dsn", None) is None:
            return self
        found = getattr(self, "_test_owner", None)
        if found is None:
            found = cls.__new__(cls)
            original_init(found, self._test_owner_dsn, schema=self.schema, pool_size=2)
            self._test_owner = found
        return found

    def close(self):  # type: ignore[no-untyped-def]
        found = getattr(self, "_test_owner", None)
        if found is not None:
            original_close(found)
        original_close(self)

    cls.__init__ = init  # type: ignore[method-assign]
    cls.migrate = migrate  # type: ignore[method-assign]
    cls.close = close  # type: ignore[method-assign]
    cls._test_owner_of = owner  # type: ignore[attr-defined]


def owner_of(database: Any) -> Any:
    """The owner role's view of ``database`` (same schema): for test checks and test setup that a runtime role may not
    do (DDL, reading Axentra's rows without a scope). Without ``NODESTRO_TEST_RUNTIME_ROLE`` it is ``database``."""
    found = getattr(database, "_test_owner_of", None)
    return database if found is None else found()


def restore_dump(conninfo: str, dump: str) -> None:
    """Run the plain pg_dump script ``LEGACY / dump`` with psycopg (no psql needed): statements as they are, COPY
    blocks through ``copy``; psql meta-commands (``\\restrict``/``\\unrestrict`` of pg_dump 17.6+) are not SQL and are
    left out."""
    import psycopg

    lines = gzip.decompress((LEGACY / dump).read_bytes()).decode("utf-8").splitlines(keepends=True)
    with psycopg.connect(conninfo, autocommit=True) as connection:
        pending: list[str] = []
        position = 0
        while position < len(lines):
            line = lines[position]
            position += 1
            if line.startswith("\\"):
                continue
            if line.startswith("COPY ") and line.rstrip().endswith("FROM stdin;"):
                if "".join(pending).strip():
                    connection.execute("".join(pending))
                pending = []
                rows: list[str] = []
                while lines[position].rstrip("\n") != "\\.":
                    rows.append(lines[position])
                    position += 1
                position += 1
                with connection.cursor() as cursor, cursor.copy(line.rstrip().rstrip(";")) as copy:
                    copy.write("".join(rows))
                continue
            pending.append(line)
        if "".join(pending).strip():
            connection.execute("".join(pending))


def has_pgvector() -> bool:
    """Whether the test server can install pgvector (``pgvector/pgvector:pg17``). Skips without a test server, before
    psycopg is imported (a plain install has none)."""
    dsn = require_postgres()
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as admin:
        row = admin.execute("SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector')").fetchone()
    return bool(row and row[0])


@contextmanager
def restored_database(dump: str) -> Iterator[str]:
    """A new database on the test server with the legacy dump ``dump`` restored into it (its connection string, as
    the owner); dropped afterwards. FX-PG-0v is skipped on a server without pgvector (a failure under
    ``NODESTRO_REQUIRE_PGVECTOR=1``). Skips without a test server before psycopg is imported."""
    dsn = require_postgres()
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    if dump == "FX-PG-0v.sql.gz" and not has_pgvector():
        require_pgvector_or_skip("FX-PG-0v needs pgvector/pgvector:pg17")
    name = f"nodestro_fx_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    parts = conninfo_to_dict(dsn)
    parts["dbname"] = name
    conninfo = make_conninfo("", **parts)
    try:
        restore_dump(conninfo, dump)
        yield conninfo
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def require_pgvector() -> None:
    """Fail under ``NODESTRO_REQUIRE_PGVECTOR=1`` (the CI job ``pg-b``): the test schema must have pgvector there."""
    if os.environ.get("NODESTRO_REQUIRE_PGVECTOR") == "1":
        pytest.fail("NODESTRO_REQUIRE_PGVECTOR=1 but the test schema has no pgvector")


def require_pgvector_or_skip(reason: str) -> None:
    """Skip a test that needs pgvector, or fail it under ``NODESTRO_REQUIRE_PGVECTOR=1`` (the CI job ``pg-b``)."""
    if os.environ.get("NODESTRO_REQUIRE_PGVECTOR") == "1":
        pytest.fail(f"NODESTRO_REQUIRE_PGVECTOR=1 but the test server has no pgvector: {reason}")
    pytest.skip(f"the test server has no pgvector: {reason}")


class FaultProxy:
    """A TCP proxy in front of the test server that loses one statement's traffic on purpose (JQ-1, JQ-3, RT2-03).

    :meth:`arm` names a statement by a part of its SQL text (as the client sends it: psycopg turns ``%s`` into
    ``$1``, ``$2`` …). The next client message that carries it (a simple ``Query`` or the ``Parse`` of the extended
    protocol) fires the fault once, on that connection only:

    - ``lose="reply"``: the statement goes to the server, which executes it (and, outside a transaction, commits it);
      every server message from then on, up to and including the ``ReadyForQuery`` that ends it, is dropped, and
      then both sides of the connection are closed. The client sees a lost connection after its statement ended.
    - ``lose="inflight"``: the statement (with the rest of the client's batch) goes to the server, and both sides
      are closed at once, without waiting for the server. The client sees a lost connection while the statement may
      still run; the server runs what it received and commits it, and its process ends when it next writes to the
      closed connection.
    - ``lose="request"``: the message is never forwarded; both sides are closed at once. The client sees a lost
      connection and the server never saw the statement.

    ``then`` runs once in the proxy, after the server ended the statement and before the client sees the cut
    (``reply``), or right after the cut (``inflight``, ``request``): a concurrent writer, or the release of a lock
    the statement waits for. :attr:`fired_backend` is the server process of the connection the fault fired on (from
    its ``BackendKeyData``).

    The proxy follows the protocol's message framing (``sslmode`` and ``gssencmode`` are disabled in :meth:`dsn`, so
    every server message is typed from the first byte), so the cut is placed by the protocol, never by a timer:
    nothing here sleeps or polls. Every other connection and statement passes through unchanged, and the client's
    new connections (a retry, the pool refilling) go through the proxy too.
    """

    MODES = ("reply", "inflight", "request")

    def __init__(self, upstream_host: str, upstream_port: int) -> None:
        self._upstream = (upstream_host, upstream_port)
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port: int = self._listener.getsockname()[1]
        self._lock = threading.Lock()
        self._armed: tuple[bytes, str, Callable[[], None] | None] | None = None
        #: How often a fault fired, and how often a lost reply was cut after the server ended the statement.
        self.fired = 0
        self.replies_lost = 0
        self.fired_backend: int | None = None
        #: An exception ``then`` raised (re-raised by the test through :meth:`check`).
        self.error: BaseException | None = None
        self._closing = False
        self._open: set[Any] = set()
        self._threads: list[threading.Thread] = []
        self._accepting = threading.Thread(target=self._accept, name="fault-proxy-accept", daemon=True)
        self._accepting.start()

    def dsn(self, base: str) -> str:
        """``base`` with this proxy as its server (no TLS, no GSS encryption: the proxy reads the plain protocol)."""
        from psycopg.conninfo import conninfo_to_dict, make_conninfo

        parts = conninfo_to_dict(base)
        parts.pop("hostaddr", None)
        parts.update(host="127.0.0.1", port=str(self.port), sslmode="disable", gssencmode="disable")
        return make_conninfo("", **parts)

    def arm(self, statement: bytes, *, lose: str = "reply", then: Callable[[], None] | None = None) -> None:
        """Fire once on the next client message whose SQL contains ``statement`` (see the class documentation)."""
        if lose not in self.MODES:
            raise ValueError(f"lose must be one of {self.MODES}, not {lose!r}")
        with self._lock:
            self._armed = (statement, lose, then)

    @property
    def armed(self) -> bool:
        with self._lock:
            return self._armed is not None

    def check(self) -> None:
        """Re-raise what ``then`` raised."""
        if self.error is not None:
            raise self.error

    def close(self) -> None:
        with self._lock:
            self._closing = True
            sockets = list(self._open)
        # Wake the blocked accept() portably: a connection to ourselves, which the accept loop sees and drops.
        with suppress(OSError):
            socket.create_connection(("127.0.0.1", self.port)).close()
        self._accepting.join(5)
        self._listener.close()
        for sock in sockets:
            self._cut(sock)
        for thread in self._threads:
            thread.join(5)

    # ---- the proxy -----------------------------------------------------------------------------

    def _accept(self) -> None:
        while True:
            try:
                client, _address = self._listener.accept()
            except OSError:
                return
            with self._lock:
                closing = self._closing
            if closing:
                client.close()
                return
            try:
                server = socket.create_connection(self._upstream)
            except OSError:
                client.close()
                continue
            link = _Link(client, server)
            with self._lock:
                self._open.update((client, server))
            for target, name in ((self._client_to_server, "up"), (self._server_to_client, "down")):
                thread = threading.Thread(target=target, args=(link,), name=f"fault-proxy-{name}", daemon=True)
                self._threads.append(thread)
                thread.start()

    def _take(self, link: _Link, kind: bytes, body: bytes) -> str | None:
        """The fault to fire for this client message, if it is the armed statement (disarms it)."""
        if kind not in (b"Q", b"P"):
            return None
        with self._lock:
            if self._armed is None or self._armed[0] not in body:
                return None
            _statement, lose, link.then = self._armed
            self._armed = None
            self.fired += 1
            self.fired_backend = link.backend
            return lose

    def _run_then(self, link: _Link) -> None:
        then, link.then = link.then, None
        if then is None:
            return
        try:
            then()
        except BaseException as exc:  # reported to the test by check()
            self.error = exc

    def _client_to_server(self, link: _Link) -> None:
        buffer = b""
        started = False  # the first message (startup, cancel request) has no type byte
        fault: str | None = None
        try:
            while True:
                data = link.client.recv(65536)
                if not data:
                    break
                buffer += data
                out = b""
                while True:
                    if not started:
                        if len(buffer) < 4:
                            break
                        size = int.from_bytes(buffer[:4], "big")
                        if len(buffer) < size:
                            break
                        out, buffer, started = out + buffer[:size], buffer[size:], True
                        continue
                    if len(buffer) < 5:
                        break
                    size = 1 + int.from_bytes(buffer[1:5], "big")
                    if len(buffer) < size:
                        break
                    message, buffer = buffer[:size], buffer[size:]
                    fault = self._take(link, message[:1], message[5:])
                    if fault == "request":
                        if out:
                            link.server.sendall(out)
                        return
                    if fault == "inflight":
                        link.server.sendall(out + message + buffer)  # the whole batch, through its Sync
                        return
                    if fault == "reply":
                        link.dropping.set()  # before the statement can reach the server
                    out += message
                if out:
                    link.server.sendall(out)
        except OSError:
            pass
        finally:
            self._close(link)
            if fault in ("request", "inflight"):
                self._run_then(link)

    def _server_to_client(self, link: _Link) -> None:
        buffer = b""
        try:
            while True:
                data = link.server.recv(65536)
                if not data:
                    break
                buffer += data
                out = b""
                while len(buffer) >= 5:
                    size = 1 + int.from_bytes(buffer[1:5], "big")
                    if len(buffer) < size:
                        break
                    message, buffer = buffer[:size], buffer[size:]
                    if message[:1] == b"K" and size >= 9:  # BackendKeyData: the server process id
                        link.backend = int.from_bytes(message[5:9], "big")
                    if not link.dropping.is_set():
                        out += message
                        continue
                    if message[:1] == b"Z":  # the server ended the statement: it ran (and committed) without us
                        if out:
                            link.client.sendall(out)
                        self._run_then(link)
                        with self._lock:
                            self.replies_lost += 1
                        return
                if out:
                    link.client.sendall(out)
        except OSError:
            pass
        finally:
            self._close(link)

    def _close(self, link: _Link) -> None:
        for sock in (link.client, link.server):
            self._cut(sock)
        with self._lock:
            self._open.difference_update((link.client, link.server))

    def _cut(self, sock: Any) -> None:
        with suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)
        with suppress(OSError):
            sock.close()


class _Link:
    """One proxied connection: the client's socket, the server's socket, whether its replies are being lost, its
    server process, and the ``then`` of a fault that fired on it."""

    def __init__(self, client: Any, server: Any) -> None:
        self.client = client
        self.server = server
        self.dropping = threading.Event()
        self.backend: int | None = None
        self.then: Callable[[], None] | None = None


def backend_gone(dsn: str, backend: int) -> bool:
    """Whether the server process ``backend`` has ended (one look, no waiting)."""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as connection:
        return connection.execute("SELECT 1 FROM pg_stat_activity WHERE pid = %s", (backend,)).fetchone() is None


@pytest.fixture
def fault_proxy() -> Iterator[FaultProxy]:
    """A :class:`FaultProxy` in front of ``NODESTRO_TEST_POSTGRES`` (skips without a test server)."""
    from psycopg.conninfo import conninfo_to_dict

    parts = conninfo_to_dict(require_postgres())
    proxy = FaultProxy(str(parts.get("hostaddr") or parts.get("host") or "127.0.0.1"), int(parts.get("port") or 5432))
    try:
        yield proxy
    finally:
        proxy.close()
