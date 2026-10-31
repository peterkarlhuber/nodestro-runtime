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

from decimal import Decimal
import threading
from typing import Any, Literal

from orkexa import UsageRecord
from orkexa.dynamic import BudgetTracker
from orkexa.pricing import PriceLookup

from .pricing import PricingService
from .problems import NodestroError

#: ``standard``: unknown prices and stale prices only warn (the default). ``strict``: a request without a known
#: price, or with a price not checked for longer than ``critical_after``, is not sent; a request whose cost
#: turned out unknown or incomplete stops the operation after it was recorded.
PriceMode = Literal["standard", "strict"]
PRICE_MODES: tuple[str, ...] = ("standard", "strict")


class PricedTracker(BudgetTracker):
    """A budget tracker priced with the workspace's catalog."""

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
        super().__init__(limit_usd, price_resolver=pricing.resolver() if pricing is not None else None, **kwargs)
        self.pricing = pricing
        self.mode: PriceMode = mode
        self._stale: set[int] = set()
        self._stale_lock = threading.Lock()
        self._entry_status: dict[tuple[str, str], tuple[str, str]] = {}

    def _status(self, provider: str, model: str) -> tuple[str, str]:
        """(status, staleness) of a model's price, looked up once per tracker (one operation)."""
        key = (provider, model)
        found = self._entry_status.get(key)
        if found is None:
            entry = self.pricing.entry(provider, model) if self.pricing is not None else None
            found = ("unknown", "ok") if entry is None else (entry.status, entry.staleness)
            self._entry_status[key] = found
        return found

    def check_request(self, *, agent: str, provider: str, model: str, lookup: PriceLookup) -> None:
        super().check_request(agent=agent, provider=provider, model=model, lookup=lookup)
        if self.mode == "strict" and lookup.price is not None and self.pricing is not None:
            _, staleness = self._status(lookup.price.provider, lookup.price.model)
            if staleness == "critical":
                raise NodestroError(
                    "pricing.stale_price",
                    f"The price of {provider}:{model} has not been checked for too long; "
                    "the request was not sent (strict price mode)",
                    model=f"{provider}:{model}",
                )

    def note(self, record: UsageRecord) -> None:
        """Remember whether ``record`` was priced with a stale price."""
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


__all__ = ["PRICE_MODES", "PriceMode", "PricedTracker", "stale_requests"]
