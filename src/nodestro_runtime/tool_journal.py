"""A durable note of every tool call an agent starts, written before the call runs (audit B-02).

An agent node keeps its tool call records in memory while it runs and the run document is stored when the node
ends. Calls the per-call gate asked about are stored at once, but every other call (all calls while approvals are
off, read tools, pre-granted calls) would leave no trace if the process died while or after it ran: the interrupted
run would show a node without calls, and continuing it (:func:`nodestro_runtime.resume.plan`) would call the tool
again without asking for a confirmation.

So before such a call runs, one small document is written to a collection of its own (``tool_call_journal``, key
``<run id>:<node id>:<iteration>:<call id>``): the tool's name, the call id, the time and a digest of the
arguments, never the arguments themselves. It is one insert of a few hundred bytes per call instead of rewriting
the whole run document, whose size grows with every output of the run. The notes of a node are deleted once the
node's state, with its call records, is stored (:func:`clear_node`); what remains belongs to a node that never got
that far, which is exactly what :func:`nodestro_runtime.resume.plan` needs to see. A deleted run's notes are deleted
with its other per-run documents (:meth:`nodestro_runtime.engine.RunEngine.discard_tool_waits`).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field

from .schemas import now
from .storage import DocumentStore

COLLECTION = "tool_call_journal"


class StartedCall(BaseModel):
    run_id: str
    node_id: str
    iteration: list[int] = Field(default_factory=list)
    call_id: str
    name: str
    #: SHA-256 of the arguments as JSON (sorted keys): tells two calls apart without storing what they contained.
    arguments_sha256: str = ""
    started_at: datetime = Field(default_factory=now)


def key(run_id: str, node_id: str, iteration: Sequence[int], call_id: str) -> str:
    return f"{run_id}:{node_id}:{'.'.join(str(item) for item in iteration)}:{call_id}"


def digest(arguments: Any) -> str:
    text = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record(
    store: DocumentStore, run_id: str, node_id: str, iteration: Sequence[int], call_id: str, name: str, arguments: Any
) -> None:
    """Note that the call starts now; the store commits it before this returns."""
    note = StartedCall(
        run_id=run_id,
        node_id=node_id,
        iteration=list(iteration),
        call_id=call_id,
        name=name,
        arguments_sha256=digest(arguments),
    )
    store.put(COLLECTION, key(run_id, node_id, iteration, call_id), note)


def started(store: DocumentStore, run_id: str, node_id: str | None = None) -> list[StartedCall]:
    """The noted calls of a run (of one node with ``node_id``) whose node state was not stored afterwards."""
    found = store.list_prefix(COLLECTION, f"{run_id}:", StartedCall)
    # Filtered by the field, not the key: a node id may contain the separator.
    return [item for item in found if item.run_id == run_id and (node_id is None or item.node_id == node_id)]


def clear_node(store: DocumentStore, run_id: str, node_id: str) -> None:
    """The node's state with its call records is stored: its notes are no longer needed."""
    for item in started(store, run_id, node_id):
        store.delete(COLLECTION, key(item.run_id, item.node_id, item.iteration, item.call_id))


def delete_run(store: DocumentStore, run_id: str) -> int:
    return store.delete_prefix(COLLECTION, f"{run_id}:")


__all__ = ["COLLECTION", "StartedCall", "clear_node", "delete_run", "digest", "key", "record", "started"]
