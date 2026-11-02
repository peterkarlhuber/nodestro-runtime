"""Prometheus metrics of the server profile, on Axentra's metrics registry (see ``docs/AXENTRA.md``, "Observability").

Nodestro builds no metrics infrastructure of its own: :class:`ServerMetrics` registers Nodestro's metrics on an
:class:`axentra.observability.metrics.MetricsRegistry` (dependency-free, Prometheus text format 0.0.4) and renders
it for ``GET /api/metrics`` (:mod:`nodestro_api.server_app`). The endpoint is off unless the operator sets
``NODESTRO_METRICS_TOKEN`` (or ``_FILE``); scrapers send it as a bearer token (see :class:`MetricsConfig`).

What is measured, never per tenant, user, run or workflow (every label value comes from a small fixed set):

- connection pools by name (``pool="nodestro"``, ``pool="axentra"``): size, idle, in use, waiting clients, errors;
- integration operations of this process (:func:`count_operation`): mirror writes by kind and outcome, identity
  resolutions by outcome, secret decryptions by format and outcome;
- read from the database at most once a minute (:class:`axentra.observability.metrics.CachedCallback`): jobs by kind
  and state, expired leases, active workers, the oldest queued job; schema versions of both runners; the
  reconciliation backlog per kind (every five minutes); mirrored record totals; runs finished and their known cost in
  the last 24 hours by state; stored tenant secrets by format and key id (key ids are not secret).

Importing this module never imports Axentra: :func:`count_operation` is plain Python, so the runtime can count from
code that also runs on the desktop. :class:`ServerMetrics` needs the ``axentra`` package (Python 3.11+).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
import hmac
import logging
from pathlib import Path
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from .axentra_platform import AxentraServices
    from .storage_postgres import PostgresDatabase

logger = logging.getLogger(__name__)

#: Shortest accepted ``NODESTRO_METRICS_TOKEN`` (characters).
MIN_TOKEN_LENGTH = 32
#: How long database-derived values are reused between scrapes (seconds).
SNAPSHOT_TTL = 60.0
#: How long the reconciliation backlog is reused (it joins Nodestro's records with Axentra's).
BACKLOG_TTL = 300.0
#: Statement timeout of the metrics queries: a slow database never ties up a scrape for long.
QUERY_TIMEOUT_MS = 5000

# ---- operation counters (no Axentra import) ----------------------------------------------------------------------

_counts: dict[tuple[str, str, str], int] = {}
_counts_lock = threading.Lock()
#: Operations and the label each one uses for its detail (anything else is folded into ``other``).
OPERATIONS: dict[str, frozenset[str]] = {
    "mirror_write": frozenset({"audit", "usage", "run_usage"}),
    "identity_resolve": frozenset({""}),
    "secret_decrypt": frozenset({"v1", "axs1"}),
    "notification": frozenset({"run_failed", "approval_pending", "budget_pending"}),
}


def count_operation(operation: str, outcome: str, *, kind: str = "", format: str = "", amount: int = 1) -> None:
    """Count one integration operation of this process (a metric, never an error: it cannot fail the caller)."""
    detail = kind or format
    allowed = OPERATIONS.get(operation)
    if allowed is None:
        operation, detail = "other", ""
    elif detail not in allowed:
        detail = "other"
    key = (operation, outcome[:40], detail)
    with _counts_lock:
        _counts[key] = _counts.get(key, 0) + amount


def operation_counts() -> dict[tuple[str, str, str], int]:
    """``(operation, outcome, detail) -> count`` since the process started."""
    with _counts_lock:
        return dict(_counts)


# ---- settings ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricsConfig:
    """``NODESTRO_METRICS_TOKEN`` (or ``_FILE``): the bearer token of ``GET /api/metrics``; unset: no endpoint (404).

    The endpoint is on the API listener behind the reverse proxy, so it is protected by the token, not by the
    network: a scraper sends ``Authorization: Bearer <token>``. Its output holds no tenant data and no secrets."""

    token: str = field(default="", repr=False)

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def authorized(self, header: str | None) -> bool:
        """Whether an ``Authorization`` header carries the token (constant-time comparison)."""
        if not self.token or not header:
            return False
        scheme, _, given = header.partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(given.strip().encode(), self.token.encode())

    @classmethod
    def from_env(cls, environ: Mapping[str, str], problems: list[dict[str, str]]) -> MetricsConfig:
        def problem(name: str, reason: str) -> None:
            problems.append({"field": name, "value": "", "reason": reason})

        direct = environ.get("NODESTRO_METRICS_TOKEN", "").strip()
        path = environ.get("NODESTRO_METRICS_TOKEN_FILE", "").strip()
        if direct and path:
            problem("NODESTRO_METRICS_TOKEN", "set either NODESTRO_METRICS_TOKEN or its _FILE, not both")
            return cls()
        token = direct
        if path:
            try:
                token = Path(path).read_text(encoding="utf-8").strip()
            except OSError as exc:
                problem("NODESTRO_METRICS_TOKEN_FILE", f"NODESTRO_METRICS_TOKEN_FILE cannot be read: {exc.strerror}")
                return cls()
        if not token:
            return cls()
        if len(token) < MIN_TOKEN_LENGTH or any(c.isspace() for c in token):
            problem(
                "NODESTRO_METRICS_TOKEN",
                f"NODESTRO_METRICS_TOKEN must be at least {MIN_TOKEN_LENGTH} characters without spaces "
                "(for example: openssl rand -hex 32)",
            )
            return cls()
        from .axentra_platform import available

        if not available():
            problem(
                "NODESTRO_METRICS_TOKEN",
                "the metrics endpoint needs the axentra package (Python 3.11+, nodestro-runtime[postgres])",
            )
            return cls()
        return cls(token=token)


# ---- the metrics -------------------------------------------------------------------------------------------------


def instrument_identity(store: Any) -> Any:
    """Count the session resolutions of an identity store (``identity_resolve``: ``valid``, ``none``, ``error``).

    Wraps ``store.session`` on the instance; the store's behaviour and results are unchanged."""
    original = store.session
    if getattr(original, "_nodestro_counted", False):
        return store

    def session(token: str) -> Any:
        try:
            found = original(token)
        except Exception:
            count_operation("identity_resolve", "error")
            raise
        count_operation("identity_resolve", "valid" if found is not None else "none")
        return found

    session._nodestro_counted = True  # type: ignore[attr-defined]
    store.session = session
    return store


class ServerMetrics:
    """Nodestro's metrics on Axentra's registry for one server process (see the module documentation).

    ``database`` is Nodestro's database; ``services`` Axentra's started services (``None`` with
    ``NODESTRO_AXENTRA=off``). ``pools`` overrides where the pools come from (default: Nodestro's pool and Axentra's,
    one series per distinct pool object, so pools merged into one are shown once)."""

    def __init__(
        self,
        database: PostgresDatabase,
        services: AxentraServices | None = None,
        *,
        pools: Callable[[], Mapping[str, Any]] | None = None,
        snapshot_ttl: float = SNAPSHOT_TTL,
        backlog_ttl: float = BACKLOG_TTL,
    ) -> None:
        from axentra.observability.metrics import CachedCallback, MetricsRegistry, register_pool_metrics

        self.database = database
        self.services = services
        self.registry = MetricsRegistry()
        register_pool_metrics(self.registry, pools or self._pools, prefix="nodestro")
        self._snapshot = CachedCallback(self._read_snapshot, snapshot_ttl)
        self._backlog = CachedCallback(self._read_backlog, backlog_ttl)
        self._register()

    # -- sources ---------------------------------------------------------------------------------------------------

    def _pools(self) -> dict[str, Any]:
        found: dict[str, Any] = {}
        seen: set[int] = set()
        candidates = [("nodestro", getattr(self.database, "_pool", None))]
        if self.services is not None:
            candidates.append(("axentra", getattr(self.services.platform.db, "pool", None)))
        for name, pool in candidates:
            if pool is not None and id(pool) not in seen:
                seen.add(id(pool))
                found[name] = pool
        return found

    @contextmanager
    def _bounded_transaction(self) -> Iterator[Any]:
        with self.database._transaction() as connection:
            connection.execute(f"SET LOCAL statement_timeout = {int(QUERY_TIMEOUT_MS)}")
            yield connection

    def _read_snapshot(self) -> dict[str, Any]:
        from .axentra_platform import stored_secret_formats
        from .storage_postgres import POSTGRES_SCHEMA_VERSION

        snapshot: dict[str, Any] = {}
        with self._bounded_transaction() as connection:
            snapshot["jobs"] = connection.execute(
                "SELECT kind, status, count(*) FROM nodestro_jobs GROUP BY kind, status"
            ).fetchall()
            leases = connection.execute(
                "SELECT count(*) FILTER (WHERE lease_until < clock_timestamp()),"
                " count(DISTINCT worker) FILTER (WHERE lease_until >= clock_timestamp()),"
                " coalesce(extract(epoch FROM clock_timestamp() - min(created_at) FILTER (WHERE status = 'queued')), 0)"
                " FROM nodestro_jobs WHERE status IN ('queued', 'running')"
            ).fetchone()
            snapshot["leases"] = leases
            snapshot["runs"] = connection.execute(
                "SELECT data ->> 'status', count(*),"
                " coalesce(sum(coalesce((data -> 'usage' ->> 'known_cost_usd')::numeric,"
                " (data -> 'usage' ->> 'estimated_cost_usd')::numeric, 0)), 0)"
                " FROM nodestro_documents WHERE collection = 'runs'"
                " AND data ->> 'status' IN ('succeeded', 'failed', 'cancelled')"
                " AND data ->> 'parent_run_id' IS NULL AND data ->> 'imported_at' IS NULL"
                " AND updated_at >= now() - interval '24 hours'"
                " AND (data ->> 'finished_at')::timestamptz >= now() - interval '24 hours'"
                " GROUP BY 1"
            ).fetchall()
        snapshot["schema"] = {"nodestro": (self.database.schema_version, POSTGRES_SCHEMA_VERSION)}
        snapshot["secrets"] = stored_secret_formats(self.database)
        if self.services is not None:
            from axentra.data.database import DbScope

            snapshot["axentra_pending"] = len(self.services.pending_migrations())
            with self.services.platform.db.transaction(DbScope.platform(), read_only=True) as tx:
                mirrored = tx.one(
                    "SELECT count(*) FILTER (WHERE idempotency_key LIKE 'nodestro:usage:%%') AS usage,"
                    " count(*) FILTER (WHERE idempotency_key LIKE 'nodestro:run:%%') AS runs"
                    " FROM axentra_usage_events WHERE product_id = %s",
                    (self.services.product_id,),
                )
                audit = tx.scalar(
                    "SELECT count(*) FROM axentra_audit_events WHERE product_id = %s AND action LIKE 'nodestro.%%'",
                    (self.services.product_id,),
                )
            snapshot["mirrored"] = {
                "usage": int(mirrored["usage"]) if mirrored else 0,
                "run_usage": int(mirrored["runs"]) if mirrored else 0,
                "audit": int(audit or 0),
            }
        return snapshot

    def _read_backlog(self) -> dict[str, int]:
        if self.services is None:
            return {}
        from .axentra_platform import reconciliation_backlog

        return reconciliation_backlog(self.services)

    def _part(self, name: str) -> Any:
        snapshot = self._snapshot() or {}
        return snapshot.get(name)

    # -- registration ----------------------------------------------------------------------------------------------

    def _register(self) -> None:
        r = self.registry
        config = self.services.config if self.services is not None else None

        def operations() -> list[tuple[dict[str, Any], float]]:
            return [
                ({"operation": op, "outcome": outcome, "detail": detail}, float(count))
                for (op, outcome, detail), count in sorted(operation_counts().items())
            ]

        r.counter_callback(
            "nodestro_integration_operations_total",
            "Integration operations of this process since it started (mirror writes, identity resolutions, secret "
            "decryptions, notifications) by outcome",
            ("operation", "outcome", "detail"),
            operations,
        )

        def switches() -> list[tuple[dict[str, Any], float]]:
            values = {
                "axentra": bool(config and config.enabled),
                "audit_mirror": bool(config and config.audit),
                "usage_mirror": bool(config and config.usage),
                "run_usage_mirror": bool(config and config.run_usage),
                "notify": bool(config and config.notify),
                "secrets_axs1": bool(config and config.secrets_format == "axs1"),
            }
            if config is not None:
                for backend in ("nodestro", "mirror", "axentra"):
                    values[f"identity_{backend}"] = config.identity_backend == backend
            return [({"switch": name}, 1.0 if on else 0.0) for name, on in values.items()]

        r.gauge(
            "nodestro_axentra_switch",
            "Axentra embedding switches of this process (1 on, 0 off)",
            ("switch",),
            callback=switches,
        )

        def jobs() -> list[tuple[dict[str, Any], float]]:
            return [
                ({"kind": str(kind), "status": str(status)}, float(n)) for kind, status, n in self._part("jobs") or []
            ]

        r.gauge("nodestro_jobs", "Jobs in the queue by kind and state", ("kind", "status"), callback=jobs)

        def lease(index: int) -> Callable[[], list[tuple[dict[str, Any], float]]]:
            def read() -> list[tuple[dict[str, Any], float]]:
                found = self._part("leases")
                return [({}, float(found[index] or 0))] if found else []

            return read

        r.gauge(
            "nodestro_jobs_expired_leases",
            "Running jobs whose lease expired (the reaper interrupts them)",
            callback=lease(0),
        )
        r.gauge("nodestro_workers_active", "Workers holding a valid lease on a running job", callback=lease(1))
        r.gauge(
            "nodestro_jobs_oldest_queued_seconds", "Age of the oldest queued job (0: none queued)", callback=lease(2)
        )

        def runs(index: int) -> Callable[[], list[tuple[dict[str, Any], float]]]:
            def read() -> list[tuple[dict[str, Any], float]]:
                return [({"status": str(row[0])}, float(row[index])) for row in self._part("runs") or []]

            return read

        r.gauge(
            "nodestro_runs_finished_24h",
            "Top-level runs finished in the last 24 hours by state",
            ("status",),
            callback=runs(1),
        )
        r.gauge(
            "nodestro_run_cost_usd_24h",
            "Known cost (USD) of the top-level runs finished in the last 24 hours, by state (all tenants)",
            ("status",),
            callback=runs(2),
        )

        def schema() -> list[tuple[dict[str, Any], float]]:
            found = self._part("schema") or {}
            out = []
            for runner, (current, expected) in found.items():
                out.append(({"runner": runner, "version": "current"}, float(current)))
                out.append(({"runner": runner, "version": "expected"}, float(expected)))
            return out

        r.gauge(
            "nodestro_schema_version",
            "Schema version of Nodestro's migration runner (current, expected)",
            ("runner", "version"),
            callback=schema,
        )

        def pending() -> list[tuple[dict[str, Any], float]]:
            value = self._part("axentra_pending")
            return [] if value is None else [({}, float(value))]

        r.gauge(
            "nodestro_axentra_migrations_pending", "Axentra migrations not applied yet (0: current)", callback=pending
        )

        def secrets() -> list[tuple[dict[str, Any], float]]:
            return [({"format": kind}, float(n)) for kind, n in (self._part("secrets") or {}).items()]

        r.gauge(
            "nodestro_secrets_stored",
            "Stored tenant secrets by format (v1, axs1:<key id>, plain)",
            ("format",),
            callback=secrets,
        )

        def mirrored() -> list[tuple[dict[str, Any], float]]:
            return [({"kind": kind}, float(n)) for kind, n in (self._part("mirrored") or {}).items()]

        r.gauge(
            "nodestro_axentra_mirrored_records",
            "Records mirrored into Axentra (all processes) by kind",
            ("kind",),
            callback=mirrored,
        )

        def backlog() -> list[tuple[dict[str, Any], float]]:
            return [({"kind": kind}, float(n)) for kind, n in (self._backlog() or {}).items()]

        r.gauge(
            "nodestro_axentra_reconcile_backlog",
            "Nodestro records not mirrored into Axentra yet (what 'axentra reconcile' would write), by kind",
            ("kind",),
            callback=backlog,
        )

    def render(self) -> str:
        """The metrics in Prometheus text format."""
        return str(self.registry.render())


__all__ = [
    "OPERATIONS",
    "MetricsConfig",
    "ServerMetrics",
    "count_operation",
    "instrument_identity",
    "operation_counts",
]
