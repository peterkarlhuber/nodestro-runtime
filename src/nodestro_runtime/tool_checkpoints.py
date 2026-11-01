"""Where an agent turn stopped at a tool call that waits for a person (M6b, ``NODESTRO_TOOL_APPROVAL_CHECKPOINTS``).

The orkexa turn checkpoint (:class:`orkexa.TurnCheckpoint`) holds the conversation of the turn up to the assistant
message with the calls, the results of the calls that already ran and the calls that wait. This module stores it,
together with the agent's run memory, as a document of its own in the tenant's store (collection
``agent_checkpoints``, key ``<run id>:<node id>:<iteration>``), never in the run document and never with tool
definitions or keys.

- **Tenant-bound:** it lives in the tenant's own store and records the tenant; another tenant's checkpoint is never
  read (:func:`load` treats it as missing).
- **Short-lived:** it expires at the latest deadline of its requests plus the execution window
  (:data:`~nodestro_runtime.tool_approvals.EXECUTION_WINDOW`), so a checkpoint restored from a backup or brought in
  any other way is expired; a different ``format`` or ``version`` is expired as well.
- **Deleted** when the agent's turn completes or fails, when the run is cancelled or deleted, and by the sweep for a
  run that is gone or not paused any more. Exports and imports of runs never contain it (another collection).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from .schemas import now
from .storage import DocumentStore

COLLECTION = "agent_checkpoints"
FORMAT = "nodestro.agent-checkpoint"
VERSION = 1


class AgentCheckpoint(BaseModel):
    format: Literal["nodestro.agent-checkpoint"] = "nodestro.agent-checkpoint"
    version: int = VERSION
    #: The store's tenant (``None`` on a desktop): a checkpoint of another tenant is never used.
    tenant_id: str | None = None
    run_id: str
    node_id: str
    iteration: list[int] = Field(default_factory=list)
    #: The orkexa turn checkpoint (``TurnCheckpoint.model_dump(mode="json")``).
    turn: dict[str, Any]
    #: The agent's run memory files when the turn stopped (``None``: the agent has no run memory).
    memory: dict[str, str] | None = None
    #: The approval requests the waiting calls belong to.
    request_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=now)
    expires_at: datetime


def key(run_id: str, node_id: str, iteration: Sequence[int]) -> str:
    return f"{run_id}:{node_id}:{'.'.join(str(item) for item in iteration)}"


def tenant_of(store: DocumentStore) -> str | None:
    return getattr(store, "tenant_id", None)


def save(store: DocumentStore, checkpoint: AgentCheckpoint) -> AgentCheckpoint:
    stamped = checkpoint.model_copy(update={"tenant_id": tenant_of(store)})
    store.put(COLLECTION, key(stamped.run_id, stamped.node_id, stamped.iteration), stamped)
    return stamped


def load(
    store: DocumentStore, run_id: str, node_id: str, iteration: Sequence[int], *, at: datetime | None = None
) -> AgentCheckpoint | None:
    """The usable checkpoint of a node's iteration; ``None`` when there is none, it belongs to another tenant, its
    format is unknown or it expired (an expired one is deleted)."""
    found_key = key(run_id, node_id, iteration)
    raw = store.get_raw(COLLECTION, found_key)
    if raw is None:
        return None
    try:
        found = AgentCheckpoint.model_validate(raw)
    except ValueError:
        store.delete(COLLECTION, found_key)
        return None
    usable = (
        found.version == VERSION
        and found.tenant_id == tenant_of(store)
        and found.run_id == run_id
        and found.expires_at > (at or now())
    )
    if not usable:
        store.delete(COLLECTION, found_key)
        return None
    return found


def delete(store: DocumentStore, run_id: str, node_id: str, iteration: Sequence[int]) -> bool:
    return store.delete(COLLECTION, key(run_id, node_id, iteration))


def delete_run(store: DocumentStore, run_id: str) -> int:
    """Delete every checkpoint of a run (cancelled, deleted, gone)."""
    return store.delete_prefix(COLLECTION, f"{run_id}:")


def runs_with_checkpoints(store: DocumentStore) -> set[str]:
    """The runs of the tenant that have a stored checkpoint (paused at a tool call)."""
    return {raw_key.split(":", 1)[0] for raw_key, _ in store.items(COLLECTION, _Any)}


class _Any(BaseModel):
    """Reads any stored document (only the keys are needed)."""

    model_config = {"extra": "allow"}


__all__ = [
    "COLLECTION",
    "FORMAT",
    "VERSION",
    "AgentCheckpoint",
    "delete",
    "delete_run",
    "key",
    "load",
    "runs_with_checkpoints",
    "save",
    "tenant_of",
]
