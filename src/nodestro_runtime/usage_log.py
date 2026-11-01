"""A ledger of AI requests made outside workflow runs.

Workflow runs keep their usage in the stored run (that stays the source for runs).
Everything else Nodestro sends to the Anthropic API is recorded here: the agent
test in the inspector, the Agent Architect, the memory assistant and the Coding
Agent that generates tools, the Workflow Architect (``workflow_design``, with
the kind of request in ``detail``) and the Nodestro Assistant (``assistant``: the
conversation in ``ref``, the answer in ``request``) and the AI judge of workflow test series
(``benchmark_judge``: the test series run in ``ref``, the test case in ``request``) and the
optional AI guide of a published API (``api_docs``: the guide in ``ref``) and the answers of the live run chat
(``run_chat``: the run in ``ref``, the question in ``request``). One entry per
operation and model, so the usage
dashboard can show tokens, cost, model and time of every request without
counting a run twice.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Literal

from orkexa import UsageRecord
from orkexa.usage import summarize_usage
from pydantic import BaseModel, Field

from .cost_tracking import stale_requests
from .engine import usage_of
from .schemas import Usage, new_id, now
from .storage import DocumentStore

UsageSource = Literal[
    "agent_test",
    "agent_design",
    "memory_proposal",
    "tool_generation",
    "workflow_design",
    "assistant",
    "benchmark_judge",
    "api_docs",
    "run_chat",
]
USAGE_SOURCES: tuple[str, ...] = (
    "agent_test",
    "agent_design",
    "memory_proposal",
    "tool_generation",
    "workflow_design",
    "assistant",
    "benchmark_judge",
    "api_docs",
    "run_chat",
)
_COLLECTION = "usage_log"


class UsageEntry(BaseModel):
    id: str = Field(default_factory=lambda: new_id("use"))
    at: datetime = Field(default_factory=now)
    source: UsageSource
    #: What the request was about, e.g. the agent's name or the tool requirement (shortened).
    label: str = ""
    model: str
    usage: Usage
    #: The id of the related object (tool job, agent test, architect job), if any.
    ref: str | None = None
    #: The kind of request within the operation, e.g. ``plan`` or ``tool`` for the Workflow Architect.
    detail: str | None = None
    #: The single request within ``ref``, e.g. the answer of an assistant conversation.
    request: str | None = None
    #: Part of the tokens are estimated (a cancelled answer whose usage the API never reported).
    estimated: bool = False


class UsageLog:
    """Stores :class:`UsageEntry` documents in the Nodestro database."""

    def __init__(self, store: DocumentStore) -> None:
        self.store = store

    def record(
        self,
        source: UsageSource,
        records: Iterable[UsageRecord],
        *,
        label: str = "",
        ref: str | None = None,
        detail: str | None = None,
        request: str | None = None,
        estimated: bool = False,
        tracker: object | None = None,
    ) -> list[UsageEntry]:
        """Store the requests of one operation, one entry per model (``provider:model``); nothing for no requests.

        Each entry keeps the cost Orkexa calculated when the request ended and its price references (snapshot
        versions, sources, quality); ``tracker`` (the operation's :class:`~.cost_tracking.PricedTracker`) adds
        which requests were priced with a stale price.
        """
        by_model: dict[str, list[UsageRecord]] = {}
        for record in records:
            model = f"{record.provider}:{record.model}" if record.provider else record.model
            by_model.setdefault(model, []).append(record)
        entries = []
        with self.store.transaction():  # one operation: all of its entries or none
            for model, group in by_model.items():
                entry = UsageEntry(
                    source=source,
                    label=label[:120],
                    model=model,
                    ref=ref,
                    detail=detail,
                    request=request,
                    estimated=estimated,
                    usage=usage_of(summarize_usage(group), group, stale_requests=stale_requests(tracker, group)),
                )
                self.store.put(_COLLECTION, entry.id, entry)
                entries.append(entry)
        return entries

    def entries(self, since: datetime | None = None) -> list[UsageEntry]:
        """All entries (newest first), optionally only those at or after ``since``."""
        found = self.store.list(_COLLECTION, UsageEntry)
        if since is not None:
            found = [entry for entry in found if entry.at >= since]
        return sorted(found, key=lambda entry: entry.at, reverse=True)
