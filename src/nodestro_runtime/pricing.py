"""Model prices: the managed price catalog of a workspace (PRICING-1, phases N1, N2 and N4).

Orkexa resolves prices and calculates costs (``orkexa.pricing``); Nodestro decides *which* prices Orkexa uses.
:class:`PricingService` keeps that catalog:

- **Reference prices** are versioned :class:`~orkexa.pricing.PriceSnapshot` documents (Orkexa's format
  ``orkexa.price-snapshot/1``) in the collection ``price_snapshots``. Exactly one is active; it is switched
  atomically (one store transaction) and earlier versions are kept for audits.
- **Overrides** (individual prices: contracts, discounts, models no source knows) live in their own collection
  ``price_overrides`` of the workspace, never inside a reference snapshot.
- **History**: every activation, rejection, review decision and override change is an entry of
  ``price_events``.

On a desktop all of it is in the workspace's database. On a server the reference snapshots belong to the
server (``reference_store``, written only by the sync job) and each tenant's overrides and events to the
tenant's store, so tenants share reference prices but never each other's overrides.

Before a candidate snapshot becomes active, :func:`validate_snapshot` checks it (ids, currency, amounts,
completeness, plausibility) and :func:`diff_snapshots` compares it with the active one. A price that changes by
more than ``review_change_ratio`` without official confirmation (quality ``verified`` or ``contract``), or a
model that disappears, is held back for review (``pending_review``); the rest is activated. A candidate that
is malformed or mostly invalid is rejected as a whole and the active prices stay as they are.

Without any stored snapshot the service starts from Orkexa's built-in prices (Anthropic list prices), so a
new or offline installation still prices Claude models. The last activated snapshot survives restarts; no
network access happens here (fetching candidates is :mod:`nodestro_runtime.pricing_sync`'s job).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from decimal import Decimal
import re
import threading
import time
from typing import Any, Literal

from orkexa.pricing import BUILTIN_SNAPSHOT, ModelPrice, PriceResolver, PriceSnapshot, PricingError
from pydantic import BaseModel, Field, ValidationError

from .problems import NodestroError
from .schemas import new_id, now
from .storage import DocumentStore

PriceStatus = Literal["current", "stale", "unknown", "pending_review", "custom"]
#: How old the last successful check of a price is: ``ok``, ``warning`` (after ``warn_after``) or ``critical``
#: (after ``critical_after``).
Staleness = Literal["ok", "warning", "critical"]
SnapshotState = Literal["active", "superseded", "pending_review", "rejected", "approved"]
EventKind = Literal[
    "activated",
    "unchanged",
    "rejected",
    "review_requested",
    "review_approved",
    "review_rejected",
    "override_set",
    "override_removed",
]

SNAPSHOTS = "price_snapshots"
STATE = "price_state"
OVERRIDES = "price_overrides"
EVENTS = "price_events"
_STATE_KEY = "reference"

#: Qualities that count as an official confirmation of a price (Orkexa's ``PriceQuality``).
CONFIRMED_QUALITIES = frozenset({"verified", "contract"})
#: The currency of every Nodestro cost report and budget; Orkexa never converts currencies.
CURRENCY = "USD"
#: Token rates above this (USD per million tokens) are treated as a data error, not as a price.
MAX_PLAUSIBLE_RATE = Decimal("10000")

_PROVIDER = re.compile(r"[a-z][a-z0-9_-]{0,39}")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}")
_RATE_FIELDS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")


class PricingSettings(BaseModel):
    """Freshness and review thresholds; the defaults are PRICING-1's."""

    #: How often prices are checked against their sources (the sync's interval).
    refresh_interval: timedelta = timedelta(hours=24)
    #: Without a successful check for this long a price is ``stale`` (warning).
    warn_after: timedelta = timedelta(hours=48)
    #: Without a successful check for this long a price is marked clearly (``critical``).
    critical_after: timedelta = timedelta(days=7)
    #: A relative change above this needs review unless the new price is officially confirmed.
    review_change_ratio: Decimal = Decimal("0.20")


class PriceIssue(BaseModel):
    """A finding of :func:`validate_snapshot`. ``error`` drops the price; ``warning`` keeps it."""

    severity: Literal["error", "warning"]
    code: str
    provider: str = ""
    model: str = ""
    message: str


class PriceChange(BaseModel):
    """One difference between the active reference prices and a candidate."""

    provider: str
    model: str
    kind: Literal["added", "removed", "changed"]
    #: The changed rate (``input``, ``output``, ...) for ``changed``; empty otherwise.
    field: str = ""
    old: str | None = None
    new: str | None = None
    #: ``|new - old| / old`` for a changed rate (``None`` when ``old`` is 0 or missing).
    ratio: str | None = None
    needs_review: bool = False


class StoredSnapshot(BaseModel):
    """A reference snapshot as kept in ``price_snapshots`` (key: its version)."""

    version: str
    content_hash: str
    source: str | None = None
    created_at: datetime = Field(default_factory=now)
    state: SnapshotState
    #: Why it was rejected or held back.
    reason: str | None = None
    activated_at: datetime | None = None
    decided_at: datetime | None = None
    #: The last successful check of each source (``ModelPrice.source``) that confirmed these prices.
    source_checks: dict[str, datetime] = Field(default_factory=dict)
    changes: list[PriceChange] = Field(default_factory=list)
    issues: list[PriceIssue] = Field(default_factory=list)
    #: The snapshot itself, in Orkexa's serialization format.
    data: dict[str, Any]

    def snapshot(self) -> PriceSnapshot:
        return PriceSnapshot.from_dict(self.data)


class PricingState(BaseModel):
    """Which reference snapshot is active, and how the last checks went (``price_state``/``reference``)."""

    active_version: str | None = None
    activated_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None


class StoredOverride(BaseModel):
    """An individual price of the workspace (``price_overrides``, key ``provider:model``)."""

    provider: str
    model: str
    price: dict[str, Any]
    updated_at: datetime = Field(default_factory=now)
    note: str | None = None

    def model_price(self) -> ModelPrice:
        return ModelPrice.from_dict(self.price)


class PricingEvent(BaseModel):
    """One entry of the price history (``price_events``)."""

    id: str = Field(default_factory=lambda: new_id("prc"))
    at: datetime = Field(default_factory=now)
    #: Wall-clock nanoseconds when the event was written: orders events with the same ``at``.
    recorded_ns: int = Field(default_factory=time.time_ns)
    kind: EventKind
    #: ``reference`` for reference prices, ``workspace`` for overrides and reviews of this workspace.
    scope: Literal["reference", "workspace"] = "reference"
    version: str | None = None
    provider: str | None = None
    model: str | None = None
    detail: str | None = None
    changes: list[PriceChange] = Field(default_factory=list)


class PriceEntry(BaseModel):
    """The effective price of one model, with everything a user needs to judge it."""

    provider: str
    model: str
    currency: str
    input_usd_per_mtok: str
    output_usd_per_mtok: str
    cache_read_usd_per_mtok: str | None = None
    cache_write_5m_usd_per_mtok: str | None = None
    cache_write_1h_usd_per_mtok: str | None = None
    has_long_context: bool = False
    tariffs: list[str] = Field(default_factory=list)
    status: PriceStatus
    staleness: Staleness
    #: Orkexa's quality of the price (``verified``, ``contract``, ``unverified``, ``estimated``).
    quality: str
    source: str
    source_url: str | None = None
    checked_at: datetime | None = None
    #: The reference snapshot version, or the override's version label for ``custom``.
    version: str | None = None
    overridden: bool = False
    #: The reference price an override replaces (``None`` when the model has none).
    reference: PriceEntry | None = None


class SubmitResult(BaseModel):
    """What :meth:`PricingService.submit` did with a candidate."""

    outcome: Literal["activated", "unchanged", "pending_review", "rejected"]
    #: The version that is active afterwards.
    active_version: str
    #: The version of the snapshot held back for review, if any.
    review_version: str | None = None
    changes: list[PriceChange] = Field(default_factory=list)
    issues: list[PriceIssue] = Field(default_factory=list)
    reason: str | None = None


class PricingStatus(BaseModel):
    """The overall state of the catalog for status displays and warnings."""

    active_version: str
    activated_at: datetime | None
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    last_error: str | None
    staleness: Staleness
    #: Prices by status.
    counts: dict[str, int]
    pending_reviews: list[str]
    overrides: int


# ---- validation ------------------------------------------------------------------------------------------


def _rates(price: ModelPrice) -> dict[str, Decimal | None]:
    return {name: getattr(price.rates, name) for name in _RATE_FIELDS}


def validate_price(price: ModelPrice, *, known_providers: Iterable[str] | None = None) -> list[PriceIssue]:
    """Check one price: ids, currency, amounts, completeness and plausibility."""
    issues: list[PriceIssue] = []

    def add(severity: Literal["error", "warning"], code: str, message: str) -> None:
        issues.append(
            PriceIssue(severity=severity, code=code, provider=price.provider, model=price.model, message=message)
        )

    if not _PROVIDER.fullmatch(price.provider):
        add("error", "pricing.invalid_provider", f"Invalid provider id {price.provider!r}")
    elif known_providers is not None and price.provider not in set(known_providers):
        add("error", "pricing.unknown_provider", f"Provider {price.provider!r} is not supported")
    if not _MODEL.fullmatch(price.model):
        add("error", "pricing.invalid_model", f"Invalid model id {price.model!r}")
    if price.currency != CURRENCY:
        add("error", "pricing.currency", f"Prices must be in {CURRENCY}, not {price.currency}")
    rates = _rates(price)
    for name, value in rates.items():
        if value is not None and value > MAX_PLAUSIBLE_RATE:
            add("error", "pricing.implausible_rate", f"{name} rate {value} USD per million tokens is implausible")
    if rates["input"] == 0 and rates["output"] == 0 and price.quality not in CONFIRMED_QUALITIES:
        # A free model is possible (local models), but an unconfirmed 0/0 is more often a placeholder.
        add("warning", "pricing.zero_price", "Input and output cost 0; is the model really free?")
    cache_read = rates["cache_read"]
    if cache_read is not None and rates["input"] is not None and cache_read > rates["input"]:
        add("warning", "pricing.cache_read_above_input", "Cache reads cost more than uncached input")
    if not price.source:
        add("error", "pricing.no_source", "The price names no source")
    return issues


def validate_snapshot(
    snapshot: PriceSnapshot, *, known_providers: Iterable[str] | None = None
) -> tuple[list[ModelPrice], list[PriceIssue]]:
    """The valid prices of ``snapshot`` and every issue found; prices with an ``error`` are left out."""
    providers = None if known_providers is None else set(known_providers)
    valid: list[ModelPrice] = []
    issues: list[PriceIssue] = []
    for price in snapshot.prices:
        found = validate_price(price, known_providers=providers)
        issues.extend(found)
        if not any(issue.severity == "error" for issue in found):
            valid.append(price)
    return valid, issues


def _ratio(old: Decimal | None, new: Decimal | None) -> Decimal | None:
    if old is None or new is None or old == 0:
        return None
    return abs(new - old) / old


def diff_snapshots(
    active: PriceSnapshot, candidate: Iterable[ModelPrice], *, review_change_ratio: Decimal, sources: set[str]
) -> list[PriceChange]:
    """The changes from ``active`` to ``candidate``, each marked when it needs review.

    Only prices of ``sources`` can be *removed*: a candidate of one source says nothing about the prices of
    another. A change needs review when a rate moves by more than ``review_change_ratio`` (or from or to an
    unknown value) and the new price is not officially confirmed; a removal always needs review.
    """
    old = {(price.provider, price.model): price for price in active.prices}
    new = {(price.provider, price.model): price for price in candidate}
    changes: list[PriceChange] = []
    for key, price in new.items():
        before = old.get(key)
        if before is None:
            changes.append(PriceChange(provider=key[0], model=key[1], kind="added"))
            continue
        confirmed = price.quality in CONFIRMED_QUALITIES
        before_rates, after_rates = _rates(before), _rates(price)
        for name in _RATE_FIELDS:
            a, b = before_rates[name], after_rates[name]
            if a == b:
                continue
            ratio = _ratio(a, b)
            large = ratio is None or ratio > review_change_ratio
            changes.append(
                PriceChange(
                    provider=key[0],
                    model=key[1],
                    kind="changed",
                    field=name,
                    old=None if a is None else str(a),
                    new=None if b is None else str(b),
                    ratio=None if ratio is None else str(ratio.quantize(Decimal("0.0001"))),
                    needs_review=large and not confirmed,
                )
            )
        if before.to_dict() != price.to_dict() and not any(c.provider == key[0] and c.model == key[1] for c in changes):
            # Tiers, tariffs, fees or metadata changed but no standard rate: a change, not a review case.
            changes.append(PriceChange(provider=key[0], model=key[1], kind="changed", field="details"))
    for key, price in old.items():
        if key not in new and price.source in sources:
            changes.append(PriceChange(provider=key[0], model=key[1], kind="removed", needs_review=True))
    return changes


# ---- the service -----------------------------------------------------------------------------------------


def _override_key(provider: str, model: str) -> str:
    return f"{provider}:{model}"


def _snapshot_record(snapshot: PriceSnapshot, state: SnapshotState, at: datetime, **fields: Any) -> StoredSnapshot:
    return StoredSnapshot(
        version=snapshot.version,
        content_hash=snapshot.content_hash(),
        source=snapshot.source,
        created_at=at,
        state=state,
        data=snapshot.to_dict(),
        **fields,
    )


class PricingService:
    """The price catalog of one workspace: reference snapshots, overrides, status and the Orkexa resolver.

    ``store`` is the workspace's store (overrides, events of the workspace); ``reference_store`` holds the
    reference snapshots, the same store on a desktop. Writes to the reference prices (:meth:`submit`,
    :meth:`approve`, :meth:`reject`) belong to the sync job; a server hands its tenants a service whose
    ``reference_writable`` is ``False``.
    """

    def __init__(
        self,
        store: DocumentStore,
        *,
        reference_store: DocumentStore | None = None,
        settings: PricingSettings | None = None,
        reference_writable: bool = True,
        known_providers: Iterable[str] | None = None,
        clock: Callable[[], datetime] = now,
        builtin: PriceSnapshot = BUILTIN_SNAPSHOT,
    ) -> None:
        self.store = store
        self.reference_store = reference_store if reference_store is not None else store
        self.settings = settings or PricingSettings()
        self.reference_writable = reference_writable
        self.known_providers = None if known_providers is None else frozenset(known_providers)
        self._clock = clock
        self._builtin = builtin
        self._lock = threading.RLock()
        self._resolver: PriceResolver | None = None
        self._resolver_key: tuple[str, str] | None = None

    # ---- reading ---------------------------------------------------------------------------------------

    def state(self) -> PricingState:
        return self.reference_store.get(STATE, _STATE_KEY, PricingState) or PricingState()

    def _active_record(self) -> StoredSnapshot | None:
        version = self.state().active_version
        if version is None:
            return None
        try:
            return self.reference_store.get(SNAPSHOTS, version, StoredSnapshot)
        except ValidationError:
            return None  # unreadable: priced with the built-in prices, see reference_snapshot()

    def reference_snapshot(self) -> PriceSnapshot:
        """The active reference snapshot; Orkexa's built-in prices until one was activated."""
        record = self._active_record()
        if record is None:
            return self._builtin
        try:
            return record.snapshot()
        except PricingError:
            # A stored snapshot this release cannot read (corrupted, or written by a newer release): never price
            # with half of it; fall back to the built-in prices and say so in the status.
            return self._builtin

    def overrides(self) -> list[StoredOverride]:
        return [override for _, override in self.store.items(OVERRIDES, StoredOverride)]

    def snapshots(self, *, limit: int | None = None) -> list[StoredSnapshot]:
        """Stored reference snapshots, most recently updated first."""
        return self.reference_store.list(SNAPSHOTS, StoredSnapshot, limit=limit)

    def snapshot(self, version: str) -> StoredSnapshot | None:
        return self.reference_store.get(SNAPSHOTS, version, StoredSnapshot)

    def history(self, *, limit: int = 200) -> list[PricingEvent]:
        """Reference and workspace events, newest first."""
        events = self.reference_store.list(EVENTS, PricingEvent)
        if self.store is not self.reference_store:
            events = [event for event in events if event.scope == "reference"]
            events += [event for event in self.store.list(EVENTS, PricingEvent) if event.scope == "workspace"]
        return sorted(events, key=lambda event: (event.at, event.recorded_ns), reverse=True)[:limit]

    def effective_snapshot(self) -> PriceSnapshot:
        """The reference snapshot with the workspace's overrides applied: the prices Orkexa uses."""
        reference = self.reference_snapshot()
        overrides = [override.model_price() for override in self.overrides()]
        if not overrides:
            return reference
        return reference.with_prices(overrides)

    def resolver(self) -> PriceResolver:
        """The Orkexa resolver of this workspace; the same instance across price changes (it is republished).

        Agents and trackers keep it; Orkexa pins one snapshot per request, so a request that runs while the
        prices change is priced consistently with the old ones.
        """
        with self._lock:
            effective = self.effective_snapshot()
            key = (effective.version, effective.content_hash())
            if self._resolver is None:
                self._resolver = PriceResolver(effective)
            elif key != self._resolver_key and effective.version not in self._resolver.versions:
                self._resolver.publish(effective)
            elif key != self._resolver_key:
                # The same version again (an override removed and set back): build a fresh resolver.
                self._resolver = PriceResolver(effective)
            self._resolver_key = key
            return self._resolver

    def _checked_at(self, price: ModelPrice, record: StoredSnapshot | None) -> datetime | None:
        candidates = [price.checked_at]
        if record is not None:
            candidates.append(record.source_checks.get(price.source))
        known = [value for value in candidates if value is not None]
        return max(known) if known else None

    def staleness(self, checked_at: datetime | None) -> Staleness:
        if checked_at is None:
            return "critical"
        age = self._clock() - checked_at
        if age > self.settings.critical_after:
            return "critical"
        if age > self.settings.warn_after:
            return "warning"
        return "ok"

    def _pending_models(self) -> set[tuple[str, str]]:
        pending: set[tuple[str, str]] = set()
        for record in self.reference_store.find(SNAPSHOTS, "state", ["pending_review"], StoredSnapshot):
            pending.update((change.provider, change.model) for change in record.changes if change.needs_review)
        return pending

    def _entry(
        self,
        price: ModelPrice,
        *,
        record: StoredSnapshot | None,
        pending: set[tuple[str, str]],
        overridden: bool = False,
        reference: PriceEntry | None = None,
    ) -> PriceEntry:
        checked_at = self._checked_at(price, record)
        staleness = self.staleness(checked_at)
        status: PriceStatus
        if overridden:
            status = "custom"
            staleness = "ok"  # an individual price is what the user set; it does not age
        elif (price.provider, price.model) in pending:
            status = "pending_review"
        elif staleness != "ok":
            status = "stale"
        else:
            status = "current"
        rates = price.rates
        return PriceEntry(
            provider=price.provider,
            model=price.model,
            currency=price.currency,
            input_usd_per_mtok=str(rates.input),
            output_usd_per_mtok=str(rates.output),
            cache_read_usd_per_mtok=None if rates.cache_read is None else str(rates.cache_read),
            cache_write_5m_usd_per_mtok=None if rates.cache_write_5m is None else str(rates.cache_write_5m),
            cache_write_1h_usd_per_mtok=None if rates.cache_write_1h is None else str(rates.cache_write_1h),
            has_long_context=bool(price.long_context),
            tariffs=[tariff.dimension for tariff in price.tariffs],
            status=status,
            staleness=staleness,
            quality=price.quality,
            source=price.source,
            source_url=price.source_url,
            checked_at=checked_at,
            version=price.version if overridden else (record.version if record else self._builtin.version),
            overridden=overridden,
            reference=reference,
        )

    def entries(self) -> list[PriceEntry]:
        """Every effective price (reference prices and overrides), sorted by provider and model."""
        record = self._active_record()
        reference = self.reference_snapshot()
        pending = self._pending_models()
        by_key = {
            (price.provider, price.model): self._entry(price, record=record, pending=pending) for price in reference
        }
        for override in self.overrides():
            key = (override.provider, override.model)
            by_key[key] = self._entry(
                override.model_price(), record=None, pending=pending, overridden=True, reference=by_key.get(key)
            )
        return [by_key[key] for key in sorted(by_key)]

    def entry(self, provider: str, model: str) -> PriceEntry | None:
        """The effective price of a model, resolved the way Orkexa resolves it (aliases, dated ids)."""
        lookup = self.effective_snapshot().lookup(provider, model)
        if lookup.price is None:
            return None
        for entry in self.entries():
            if entry.provider == lookup.price.provider and entry.model == lookup.price.model:
                return entry
        return None

    def price_status(self, provider: str, model: str) -> PriceStatus:
        entry = self.entry(provider, model)
        return "unknown" if entry is None else entry.status

    def status(self) -> PricingStatus:
        state = self.state()
        entries = self.entries()
        counts: dict[str, int] = {}
        for entry in entries:
            counts[entry.status] = counts.get(entry.status, 0) + 1
        worst: Staleness = "ok"
        for entry in entries:
            if entry.overridden:
                continue
            if entry.staleness == "critical":
                worst = "critical"
                break
            if entry.staleness == "warning":
                worst = "warning"
        pending = [
            record.version
            for record in self.reference_store.find(SNAPSHOTS, "state", ["pending_review"], StoredSnapshot)
        ]
        return PricingStatus(
            active_version=self.reference_snapshot().version,
            activated_at=state.activated_at,
            last_attempt_at=state.last_attempt_at,
            last_success_at=state.last_success_at,
            last_error=state.last_error,
            staleness=worst,
            counts=counts,
            pending_reviews=pending,
            overrides=len(self.overrides()),
        )

    # ---- reference prices (sync job) --------------------------------------------------------------------

    def _require_writable(self) -> None:
        if not self.reference_writable:
            raise NodestroError("pricing.reference_read_only", "Reference prices are managed by the server")

    def _event(self, store: DocumentStore, event: PricingEvent) -> None:
        store.put(EVENTS, event.id, event)

    def _unique_version(self, snapshot: PriceSnapshot) -> PriceSnapshot:
        existing = self.reference_store.get(SNAPSHOTS, snapshot.version, StoredSnapshot)
        if existing is None or existing.content_hash == snapshot.content_hash():
            return snapshot
        return PriceSnapshot(
            f"{snapshot.version}+{snapshot.content_hash()[:12]}",
            snapshot.prices,
            created_at=snapshot.created_at,
            source=snapshot.source,
            description=snapshot.description,
        )

    def _activate(
        self, snapshot: PriceSnapshot, at: datetime, *, changes: list[PriceChange], sources: set[str]
    ) -> None:
        """Store ``snapshot`` and make it the active one; the caller holds a store transaction."""
        state = self.state()
        previous = state.active_version
        old = self._active_record()
        if old is not None and previous != snapshot.version:
            old.state = "superseded"
            self.reference_store.put(SNAPSHOTS, old.version, old)
        record = self.reference_store.get(SNAPSHOTS, snapshot.version, StoredSnapshot)
        if record is None:
            record = _snapshot_record(snapshot, "active", at, changes=changes)
            if old is not None:
                # Prices carried over from the previous version keep the checks their sources had.
                record.source_checks = dict(old.source_checks)
        record.state = "active"
        record.activated_at = at
        record.source_checks.update({source: at for source in sources})
        self.reference_store.put(SNAPSHOTS, record.version, record)
        state.active_version = snapshot.version
        state.activated_at = at
        self.reference_store.put(STATE, _STATE_KEY, state)

    def record_attempt(self, *, error: str | None = None) -> None:
        """Note a sync attempt (successful when ``error`` is ``None``)."""
        with self.reference_store.transaction():
            state = self.state()
            at = self._clock()
            state.last_attempt_at = at
            if error is None:
                state.last_success_at = at
                state.last_error = None
            else:
                state.last_error = error[:500]
            self.reference_store.put(STATE, _STATE_KEY, state)

    def submit(self, candidate: PriceSnapshot, *, min_share: float = 0.5) -> SubmitResult:
        """Validate a candidate from one or more sources and activate what may be activated.

        - Invalid prices are dropped; a candidate whose prices are mostly invalid, or that has fewer than
          ``min_share`` of the prices the active snapshot holds from the same sources, is rejected as a whole
          (a broken or truncated download), and the active prices stay unchanged.
        - An unconfirmed price never replaces an officially confirmed price of another source.
        - Changes that need review are held back in a ``pending_review`` snapshot; the active prices keep the
          old values for those models until the review is approved.
        - Everything else is activated atomically as a new version.
        """
        self._require_writable()
        at = self._clock()
        with self._lock, self.reference_store.transaction():
            active = self.reference_snapshot()
            valid, issues = validate_snapshot(candidate, known_providers=self.known_providers)
            sources = {price.source for price in candidate.prices} | ({candidate.source} if candidate.source else set())
            errors = [issue for issue in issues if issue.severity == "error"]
            previous_count = sum(1 for price in active.prices if price.source in sources)
            reason: str | None = None
            if not candidate.prices:
                reason = "The candidate contains no prices"
            elif len(errors) > len(candidate.prices) / 2:
                reason = f"{len(errors)} of {len(candidate.prices)} prices are invalid"
            elif previous_count and len(valid) < previous_count * min_share:
                reason = f"Only {len(valid)} valid prices, the active snapshot has {previous_count} from these sources"
            if reason is not None:
                rejected = self._unique_version(candidate)
                self.reference_store.put(
                    SNAPSHOTS,
                    rejected.version,
                    _snapshot_record(rejected, "rejected", at, reason=reason, issues=issues[:200]),
                )
                self._event(
                    self.reference_store, PricingEvent(at=at, kind="rejected", version=rejected.version, detail=reason)
                )
                return SubmitResult(outcome="rejected", active_version=active.version, issues=issues, reason=reason)

            # An unconfirmed price never replaces an officially confirmed one of another source (LiteLLM's
            # Anthropic prices next to Orkexa's verified ones): the confirmed price stays, without a review case.
            confirmed = {
                (price.provider, price.model): price.source
                for price in active.prices
                if price.quality in CONFIRMED_QUALITIES
            }
            valid = [
                price
                for price in valid
                if price.quality in CONFIRMED_QUALITIES
                or confirmed.get((price.provider, price.model), price.source) == price.source
            ]
            changes = diff_snapshots(
                active, valid, review_change_ratio=self.settings.review_change_ratio, sources=sources
            )
            held = {(change.provider, change.model) for change in changes if change.needs_review}
            accepted = [price for price in valid if (price.provider, price.model) not in held]
            removed = {
                (change.provider, change.model)
                for change in changes
                if change.kind == "removed" and (change.provider, change.model) not in held
            }
            kept = [price for price in active.prices if (price.provider, price.model) not in removed]
            accepted_keys = {(price.provider, price.model) for price in accepted}
            merged = [price for price in kept if (price.provider, price.model) not in accepted_keys] + accepted
            draft = PriceSnapshot("draft", tuple(merged))
            review_version: str | None = None
            if held:
                review = self._unique_version(
                    PriceSnapshot(
                        candidate.version,
                        tuple(price for price in valid if (price.provider, price.model) in held),
                        created_at=candidate.created_at,
                        source=candidate.source,
                        description=candidate.description,
                    )
                )
                review_changes = [change for change in changes if change.needs_review]
                self.reference_store.put(
                    SNAPSHOTS,
                    review.version,
                    _snapshot_record(
                        review,
                        "pending_review",
                        at,
                        reason="Price changes need review",
                        changes=review_changes,
                        issues=issues[:200],
                    ),
                )
                review_version = review.version
                self._event(
                    self.reference_store,
                    PricingEvent(at=at, kind="review_requested", version=review.version, changes=review_changes),
                )
            applied = [change for change in changes if not change.needs_review]
            if draft.content_hash() == active.content_hash():
                record = self._active_record()
                if record is not None:
                    record.source_checks.update({source: at for source in sources})
                    self.reference_store.put(SNAPSHOTS, record.version, record)
                else:
                    # Still on the built-in prices: store them, so the confirmed check is remembered.
                    self._activate(active, at, changes=[], sources=sources)
                self._event(self.reference_store, PricingEvent(at=at, kind="unchanged", version=active.version))
                return SubmitResult(
                    outcome="pending_review" if held else "unchanged",
                    active_version=active.version,
                    review_version=review_version,
                    changes=changes,
                    issues=issues,
                )
            new = self._unique_version(
                PriceSnapshot(
                    candidate.version,
                    draft.prices,
                    created_at=at,
                    source=candidate.source,
                    description=candidate.description,
                )
            )
            self._activate(new, at, changes=applied, sources=sources)
            self._event(
                self.reference_store, PricingEvent(at=at, kind="activated", version=new.version, changes=applied)
            )
            return SubmitResult(
                outcome="pending_review" if held else "activated",
                active_version=new.version,
                review_version=review_version,
                changes=changes,
                issues=issues,
            )

    def _pending(self, version: str) -> StoredSnapshot:
        record = self.reference_store.get(SNAPSHOTS, version, StoredSnapshot)
        if record is None or record.state != "pending_review":
            raise NodestroError("pricing.review_not_found", f"No price review {version!r} is open", version=version)
        return record

    def approve(self, version: str, *, note: str | None = None) -> SubmitResult:
        """Accept the held-back changes of a review: they are activated on top of the active prices."""
        self._require_writable()
        at = self._clock()
        with self._lock, self.reference_store.transaction():
            record = self._pending(version)
            reviewed = record.snapshot()
            active = self.reference_snapshot()
            removed = {(c.provider, c.model) for c in record.changes if c.kind == "removed"}
            keys = {(price.provider, price.model) for price in reviewed.prices}
            merged = [price for price in active.prices if (price.provider, price.model) not in keys | removed] + list(
                reviewed.prices
            )
            new = self._unique_version(
                PriceSnapshot(f"{active.version}+review-{record.content_hash[:8]}", tuple(merged), created_at=at)
            )
            record.state = "approved"
            record.decided_at = at
            self.reference_store.put(SNAPSHOTS, record.version, record)
            self._activate(new, at, changes=record.changes, sources={price.source for price in reviewed.prices})
            self._event(
                self.reference_store,
                PricingEvent(at=at, kind="review_approved", version=new.version, detail=note, changes=record.changes),
            )
            return SubmitResult(outcome="activated", active_version=new.version, changes=record.changes)

    def reject(self, version: str, *, note: str | None = None) -> None:
        """Discard the held-back changes of a review; the active prices stay."""
        self._require_writable()
        at = self._clock()
        with self._lock, self.reference_store.transaction():
            record = self._pending(version)
            record.state = "rejected"
            record.decided_at = at
            record.reason = note or "Rejected in review"
            self.reference_store.put(SNAPSHOTS, record.version, record)
            self._event(self.reference_store, PricingEvent(at=at, kind="review_rejected", version=version, detail=note))

    # ---- overrides (the workspace) ----------------------------------------------------------------------

    def set_override(self, price: ModelPrice, *, note: str | None = None) -> StoredOverride:
        """Store an individual price for one model; it wins over the reference price."""
        issues = [
            issue for issue in validate_price(price, known_providers=self.known_providers) if issue.severity == "error"
        ]
        if issues:
            raise NodestroError("pricing.invalid_price", issues[0].message, issues=[issue.code for issue in issues])
        at = self._clock()
        override = StoredOverride(
            provider=price.provider, model=price.model, price=price.to_dict(), updated_at=at, note=note
        )
        with self._lock, self.store.transaction():
            self.store.put(OVERRIDES, _override_key(price.provider, price.model), override)
            self._event(
                self.store,
                PricingEvent(
                    at=at,
                    kind="override_set",
                    scope="workspace",
                    provider=price.provider,
                    model=price.model,
                    detail=note,
                ),
            )
        return override

    def remove_override(self, provider: str, model: str) -> bool:
        """Go back to the reference price; ``False`` when the model had no override."""
        at = self._clock()
        with self._lock, self.store.transaction():
            removed = self.store.delete(OVERRIDES, _override_key(provider, model))
            if removed:
                self._event(
                    self.store,
                    PricingEvent(at=at, kind="override_removed", scope="workspace", provider=provider, model=model),
                )
        return removed


__all__ = [
    "CONFIRMED_QUALITIES",
    "CURRENCY",
    "PriceChange",
    "PriceEntry",
    "PriceIssue",
    "PriceStatus",
    "PricingEvent",
    "PricingService",
    "PricingSettings",
    "PricingState",
    "PricingStatus",
    "Staleness",
    "StoredOverride",
    "StoredSnapshot",
    "SubmitResult",
    "diff_snapshots",
    "validate_price",
    "validate_snapshot",
]
