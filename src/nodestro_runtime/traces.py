"""Import exported run traces (``run_<id>.json``) so they can be viewed again.

An imported trace becomes a stored :class:`~nodestro_runtime.schemas.Run` with a new id
and ``imported_at`` set. Nothing is executed and no API request is made: the
run only shows what happened when it was recorded. Existing runs are never
overwritten, and importing the same trace twice opens the earlier import.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import ValidationError

from .problems import NodestroError
from .schemas import Run, new_id, now
from .storage import DocumentStore

MAX_TRACE_BYTES = 50 * 1024 * 1024
_FINGERPRINTS = "trace_imports"
_UNFINISHED = "Incomplete trace: the run had not finished when it was exported."


class TraceImportError(NodestroError):
    """The file is not a trace Nodestro can show."""


def fingerprint(run: Run) -> str:
    """A hash of what the trace records, independent of its id and of import metadata."""
    content = run.model_dump_json(exclude={"id", "imported_at", "source_run_id"})
    return hashlib.sha256(content.encode()).hexdigest()


def parse_trace(data: bytes) -> Run:
    """Validate an exported trace; raises :class:`TraceImportError` with a code for the UI."""
    if len(data) > MAX_TRACE_BYTES:
        raise TraceImportError("trace.too_large", "The file is too large: a trace may have at most 50 MB.", limit_mb=50)
    try:
        raw: Any = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise TraceImportError("trace.not_json", "The file is not valid JSON.") from None
    if not isinstance(raw, dict):
        raise TraceImportError("trace.not_object", "The file is not a Nodestro trace: a JSON object is expected.")
    if "workflow" not in raw and isinstance(raw.get("nodes"), list) and "edges" in raw:
        raise TraceImportError(
            "trace.is_workflow", "This is an exported workflow, not a trace. Import workflows on the Workflows page."
        )
    missing = [key for key in ("id", "workflow_id", "workflow", "nodes", "status") if key not in raw]
    if missing:
        raise TraceImportError(
            "trace.missing_fields",
            f"The file is not a Nodestro trace: the fields {', '.join(missing)} are missing.",
            fields=", ".join(missing),
        )
    try:
        return Run.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()[:3]
        )
        raise TraceImportError(
            "trace.invalid", f"The trace is damaged or incomplete ({problems}).", details=problems
        ) from None


def find_duplicate(store: DocumentStore, trace: Run) -> Run | None:
    """The stored run this trace duplicates: the original run itself or an earlier import."""
    key = fingerprint(trace)
    original = store.get("runs", trace.id, Run)
    if original is not None and fingerprint(original) == key:
        return original
    earlier = store.get_raw(_FINGERPRINTS, key)
    if isinstance(earlier, str):
        return store.get("runs", earlier, Run)  # None when that import was deleted meanwhile
    return None


def import_trace(store: DocumentStore, data: bytes) -> tuple[Run, bool]:
    """Store a trace as a new, read-only run; returns ``(run, duplicate)``."""
    trace = parse_trace(data)
    duplicate = find_duplicate(store, trace)
    if duplicate is not None:
        return duplicate, True
    run = trace.model_copy(
        deep=True,
        update={"id": new_id("run"), "source_run_id": trace.source_run_id or trace.id, "imported_at": now()},
    )
    if run.status in ("queued", "running"):
        # A trace exported mid-run cannot continue here; show it as stopped.
        run.status = "cancelled"
        if run.error is None:
            run.error, run.error_code = _UNFINISHED, "trace.unfinished"
        for state in run.nodes.values():
            if state.status in ("waiting", "running"):
                state.status = "cancelled"
    store.put("runs", run.id, run)
    store.put_raw(_FINGERPRINTS, fingerprint(trace), run.id)
    _import_chat(store, data, trace.id, run.id)
    return run, False


def _import_chat(store: DocumentStore, data: bytes, old_id: str, new_id_: str) -> None:
    """The live run chat an exported trace carries (``chat``, LRC-1), stored with the imported run (read-only)."""
    from .run_chat import COLLECTION, MAX_MESSAGES_IMPORTED, RunMessage, message_key

    raw = json.loads(data)
    items = raw.get("chat") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return
    for item in items[:MAX_MESSAGES_IMPORTED]:
        try:
            message = RunMessage.model_validate(item)
        except ValidationError:
            continue  # a damaged message is left out; the trace itself is valid
        message = message.model_copy(
            update={
                "run_id": new_id_,
                "target_run_id": new_id_ if message.target_run_id == old_id else message.target_run_id,
                "client_id": None,
            }
        )
        store.put(COLLECTION, message_key(new_id_, message.seq), message)
