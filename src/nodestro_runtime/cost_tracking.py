"""Usage trackers that price through the workspace's price catalog (PRICING-1, phases N5 and N6).

Every AI request Nodestro makes is priced by Orkexa against the snapshot an agent pins for it; which prices
Orkexa sees is decided by the tracker's ``price_resolver``. :class:`PricedTracker` is the
:class:`~orkexa.dynamic.BudgetTracker` of every Nodestro operation (workflow runs, sub-workflows, agent tests,
the AI assistants, tool generation, benchmark judges): it hands Orkexa the workspace's resolver
(:meth:`PricingService.resolver`), remembers which requests were priced with a stale price, and, in the strict
price mode, refuses requests whose price is unknown or critically outdated before they are sent.

Costs are calculated once, when a request ends, and stored with the request's usage; later price changes never
alter them.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextvars import ContextVar
from decimal import Decimal
import threading
from typing import TYPE_CHECKING, Any, Literal

from orkexa import Reservation, UsageRecord
from orkexa.dynamic import BudgetExceededError, BudgetTracker, UnpricedModelError
from orkexa.pricing import ModelPrice, PriceLookup

from .pricing import REGIONAL_PROVIDERS, PricingService, missing_conditions
from .problems import NodestroError

if TYPE_CHECKING:
    from .run_budget import RunBudgetGate, TrackerScope

#: Set while the outermost tracker of a request admits it: the parents it passes the reservation on to do not hold
#: the request again (BUDGET-1).
_ADMITTING: ContextVar[bool] = ContextVar("nodestro_budget_admitting", default=False)

#: ``standard``: unknown prices and stale prices only warn (the default). ``strict``: a request without a known
#: price, or with a price not checked for longer than ``critical_after``, is not sent; a request whose cost
#: turned out unknown or incomplete stops the operation after it was recorded. For a provider priced per region
#: (Amazon Bedrock, :data:`~nodestro_runtime.pricing.REGIONAL_PROVIDERS`) "a known price" means a price that covers
#: the region of the workspace's connection; a price for other regions only is refused before the request too.
PriceMode = Literal["standard", "strict"]
PRICE_MODES: tuple[str, ...] = ("standard", "strict")


class PricedTracker(BudgetTracker):
    """A budget tracker priced with the workspace's catalog.

    **Budget decisions (BUDGET-1).** The trackers of an attended run carry the run's
    :class:`~nodestro_runtime.run_budget.RunBudgetGate` (``budget_gate``) and the run whose limit they hold
    (``budget_scope``). For them a refused request is not an error of the run: the gate opens one budget request for
    the run and stops the requesting turn before anything is sent (:class:`orkexa.RequestSuspended`); while it is
    pending every further request of the run waits too. The limit check before a request therefore happens where
    the request's most expensive outcome is known (:meth:`try_reserve`), and such trackers never raise after a
    request was recorded: the paid response is used, and the next request waits for the decision.
    """

    #: The run's budget decisions (attended runs only) and the run whose limit this tracker holds.
    budget_gate: RunBudgetGate | None = None
    budget_scope: TrackerScope | None = None

    def __init__(
        self,
        limit_usd: Decimal | float | str | None = None,
        *,
        pricing: PricingService | None = None,
        mode: PriceMode = "standard",
        **kwargs: Any,
    ) -> None:
        if mode == "strict":
            kwargs.setdefault("on_unpriced", "reject")
            # Strict: a request whose most expensive outcome does not fit the rest of the budget is refused.
            kwargs.setdefault("on_oversized", "reject")
        super().__init__(limit_usd, price_resolver=pricing.resolver() if pricing is not None else None, **kwargs)
        self.pricing = pricing
        self.mode: PriceMode = mode
        self._stale: set[int] = set()
        self._stale_lock = threading.Lock()
        self._entry_status: dict[tuple[str, str], tuple[str, str]] = {}
        #: Request conditions fixed when an agent was built (Bedrock: the region its client was made for), by agent
        #: name; they win over the workspace's current settings, which may change while the agent runs.
        self._agent_conditions: dict[str, dict[str, str]] = {}
        #: The region each agent's client was built for (Bedrock), by agent name as this tracker's records name it;
        #: passed on to the parent trackers under their names, so every ledger can record it (RT-015).
        self._agent_regions: dict[str, str] = {}
        #: The region of each recorded request of a regional provider, by ``id(record)`` (see :meth:`region_of`).
        self._regions: dict[int, str] = {}

    def pin_conditions(self, agent: str, options: Any) -> None:
        """Remember the conditions of an agent's requests from its options (Bedrock: ``region_name``)."""
        region = getattr(options, "region_name", None) if getattr(options, "provider", None) == "bedrock" else None
        if isinstance(region, str) and region:
            self._agent_conditions[agent] = {"aws_region": region}
            self._pin_region(agent, region)

    def _pin_region(self, agent: str, region: str) -> None:
        tracker: Any = self
        while isinstance(tracker, PricedTracker):
            with tracker._stale_lock:
                tracker._agent_regions[agent] = region
            agent = tracker._upstream(agent)
            tracker = tracker.parent

    def _status(self, provider: str, model: str) -> tuple[str, str]:
        """(status, staleness) of a model's price, looked up once per tracker (one operation)."""
        key = (provider, model)
        found = self._entry_status.get(key)
        if found is None:
            entry = self.pricing.entry(provider, model) if self.pricing is not None else None
            found = ("unknown", "ok") if entry is None else (entry.status, entry.staleness)
            self._entry_status[key] = found
        return found

    def _uncovered(self, provider: str, price: ModelPrice, agent: str = "") -> list[str]:
        """The known request conditions (Bedrock: the region the agent was built for, else the connection's
        current region) that ``price`` does not cover."""
        dimension = REGIONAL_PROVIDERS.get(provider)
        if dimension is None:
            return []
        known = self._agent_conditions.get(agent) or (
            self.pricing.request_conditions(provider) if self.pricing is not None else {}
        )
        value = known.get(dimension)
        if not value:
            return [f"tariff:{dimension}=unknown"]  # the region is not known: the price cannot be shown to fit
        return missing_conditions(price, {dimension: value})

    def check_request(self, *, agent: str, provider: str, model: str, lookup: PriceLookup) -> None:
        super().check_request(agent=agent, provider=provider, model=model, lookup=lookup)
        if self.mode == "strict" and lookup.price is not None:
            uncovered = self._uncovered(provider, lookup.price, agent)
            if uncovered:
                # Orkexa's pre-check only asks whether a price exists; its cost would still be unknown here.
                raise UnpricedModelError(
                    f"No price known for {provider}:{model} under {', '.join(uncovered)} (agent {agent!r}); "
                    "the request was not sent (strict price mode)"
                )
        if self.mode == "strict" and lookup.price is not None and self.pricing is not None:
            _, staleness = self._status(lookup.price.provider, lookup.price.model)
            if staleness == "critical":
                raise NodestroError(
                    "pricing.stale_price",
                    f"The price of {provider}:{model} has not been checked for too long; "
                    "the request was not sent (strict price mode)",
                    model=f"{provider}:{model}",
                )

    # ---- budget decisions (BUDGET-1) ------------------------------------------------------------------------------

    def before_request(self, *, agent: str, provider: str, model: str, priced: bool) -> None:
        if self.budget_gate is None:
            super().before_request(agent=agent, provider=provider, model=model, priced=priced)
            return
        # The limit is checked by try_reserve, which also knows the request's cost bound; the other checks stay.
        if not priced and self.on_unpriced == "reject":
            raise UnpricedModelError(
                f"No price known for {provider}:{model} (agent {agent!r}); the budget cannot be enforced"
            )
        if self.parent is not None:
            upstream = f"{self.prefix}/{agent}" if self.prefix else agent
            self.parent.before_request(agent=upstream, provider=provider, model=model, priced=priced)

    def try_reserve(self, *, agent: str, provider: str, model: str, amount: Decimal | None) -> Reservation | None:
        gate = self.budget_gate
        if gate is None or _ADMITTING.get():
            return super().try_reserve(agent=agent, provider=provider, model=model, amount=amount)
        token = _ADMITTING.set(True)
        try:
            gate.hold(self, agent=agent, provider=provider, model=model, amount=amount)
            try:
                return super().try_reserve(agent=agent, provider=provider, model=model, amount=amount)
            except UnpricedModelError:
                raise  # a price problem: a higher budget cannot fix it
            except BudgetExceededError as exc:
                blocking = getattr(exc, "tracker", None)
                if not isinstance(blocking, PricedTracker) or blocking.budget_scope is None:
                    raise
                gate.block(blocking, exc, agent=agent, provider=provider, model=model, amount=amount, requester=self)
                raise  # not reached: block() stops the turn or raises exc
        finally:
            _ADMITTING.reset(token)

    def check(self) -> None:
        if self.budget_gate is not None:
            return  # recorded costs never end an attended run: its next request waits for a decision instead
        super().check()

    def _request_region(self, record: UsageRecord) -> str | None:
        """The region ``record``'s request went to, for a provider priced per region (Bedrock); ``None`` otherwise or
        when it is not known. The region the agent's client was built for wins, then the region its price was
        chosen for, then the region of the workspace's connection (what the strict price check uses too)."""
        dimension = REGIONAL_PROVIDERS.get(record.provider or "")
        if dimension is None:
            return None
        found = self._agent_regions.get(record.agent) or dict(record.pricing_conditions).get(dimension)
        if not found and self.pricing is not None:
            found = self.pricing.request_conditions(record.provider or "").get(dimension)
        return found if isinstance(found, str) and found else None

    def region_of(self, record: UsageRecord) -> str | None:
        """The region recorded for ``record`` (one of this tracker's records) by :meth:`add`; ``None`` if none."""
        return self._regions.get(id(record))

    def note(self, record: UsageRecord) -> None:
        """Remember the region of ``record``'s request and whether it was priced with a stale price."""
        region = self._request_region(record)
        if region is not None:
            with self._stale_lock:
                self._regions[id(record)] = region
        if self.pricing is None or record.pricing_source is None or not record.provider:
            return
        status, _ = self._status(record.provider, record.model)
        if status == "unknown" and record.requested_model:
            status, _ = self._status(record.provider, record.requested_model)
        if status == "stale":
            with self._stale_lock:
                self._stale.add(id(record))

    def add(self, record: UsageRecord) -> None:
        self.note(record)
        super().add(record)

    def is_stale(self, record: UsageRecord) -> bool:
        return id(record) in self._stale

    def stale_requests(self, records: tuple[UsageRecord, ...] | list[UsageRecord] | None = None) -> int:
        """How many of ``records`` (default: all of this tracker's) were priced with a stale price."""
        chosen = self.records if records is None else records
        return sum(1 for record in chosen if id(record) in self._stale)


def stale_requests(tracker: object, records: tuple[UsageRecord, ...] | list[UsageRecord] | None = None) -> int:
    """:meth:`PricedTracker.stale_requests` for any tracker (0 for trackers that do not price with a catalog)."""
    return tracker.stale_requests(records) if isinstance(tracker, PricedTracker) else 0


def request_region(tracker: object, record: UsageRecord) -> str | None:
    """The provider region of one of ``tracker``'s records (Bedrock); ``None`` when unknown or not regional."""
    return tracker.region_of(record) if isinstance(tracker, PricedTracker) else None


def request_regions(tracker: object, records: Iterable[UsageRecord]) -> list[str]:
    """The provider regions (Bedrock) of ``records``, sorted and without repeats; empty when none is known."""
    return sorted({region for record in records if (region := request_region(tracker, record)) is not None})


__all__ = ["PRICE_MODES", "PriceMode", "PricedTracker", "request_region", "request_regions", "stale_requests"]
