"""Which content an owner's consent covers (RT-089d): the digests of workflows and agent configurations.

On a server a memory space is its owner's alone (RT-089c), but a workflow may be shared and edited by other members.
Before RT-089d a member could build a workflow that names another person's space; when that person ran it, its steps
read their memory and could hand it to any tool of the workflow. Now a step opens a space only when its owner allowed
exactly this content (:class:`~nodestro_runtime.memory_spaces.MemoryConsent`):

- **Digest.** :func:`workflow_digest` is the SHA-256 of a workflow's content as a run executes it (its id, nodes,
  edges, limits, notifications; not its time stamps or origin), with every sub-workflow and team pinned to the
  version it will run (:meth:`~nodestro_runtime.engine.RunManager.pin_subworkflows`). Any change, of an agent, a
  tool, an edge or the version of a sub-workflow, changes the digest; a consent never applies to changed content.
- **Every run that can see the memory.** A step that opens a space needs a consent for its own run's workflow and
  for every run above it (their steps get the step's output). Once a run used a space (``Run.memory_spaces`` of the
  top-level run), every run that executes afterwards in the same tree (a sub-workflow, a team, a continued paused
  run, a continued failed run) needs a consent for its workflow too. So the content can only reach workflow content
  that its owner allowed.
- **Granting.** :func:`consent_targets` lists a workflow and every workflow it runs (sub-workflows, teams,
  recursively, in the versions it would run now), which the owner allows together; the HTTP API records them.
- **Agent tests** use the digest of the tested configuration (:func:`agent_digest`).

Nothing grants a consent by itself, and the desktop (no owners) needs none.
"""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
from typing import Any

from pydantic import BaseModel

from . import graph
from .problems import NodestroError
from .schemas import AgentConfig, Workflow, WorkflowIn

#: The most workflows one consent covers (a workflow, its sub-workflows and teams); more is refused, never cut.
MAX_TARGETS = 50
#: A workflow's fields that are not its content (time stamps, where a copy came from).
_NOT_CONTENT = frozenset({"created_at", "updated_at", "origin"})


def _digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def workflow_digest(workflow: Workflow) -> str:
    """The SHA-256 of ``workflow``'s content (see the module documentation); pass it as a run executes it (pinned)."""
    fields = set(WorkflowIn.model_fields) | {"id"}
    data = workflow.model_dump(mode="json", include=fields - _NOT_CONTENT)
    return _digest(data)


def agent_digest(config: AgentConfig) -> str:
    """The SHA-256 of an agent configuration (an agent test's consent)."""
    return _digest(AgentConfig.model_validate(config.model_dump()).model_dump(mode="json"))


class ConsentTarget(BaseModel):
    """One workflow a consent covers, as it would run now."""

    workflow_id: str
    version: int | None = None
    name: str = ""
    digest: str


def consent_targets(
    resolve: Callable[[str, int | None], tuple[Workflow, int | None]],
    pin: Callable[[Workflow], Workflow],
    workflow_id: str,
    version: int | None = None,
) -> list[ConsentTarget]:
    """The workflow ``workflow_id`` (``version``; ``None``: the latest) and every workflow it runs, recursively, in
    the versions they would run now, each with its digest. ``resolve`` and ``pin`` are the engine's
    (``resolve_workflow``, ``pin_subworkflows``), so the digests are the ones its runs will have. Refused with
    ``memory.consent_too_large`` above :data:`MAX_TARGETS`; an unknown workflow raises ``KeyError``."""
    found: list[ConsentTarget] = []
    seen: set[tuple[str, int | None]] = set()
    queue: list[tuple[str, int | None]] = [(workflow_id, version)]
    while queue:
        current, wanted = queue.pop(0)
        snapshot, number = resolve(current, wanted)
        if (current, number) in seen:
            continue
        seen.add((current, number))
        pinned = pin(snapshot)
        found.append(
            ConsentTarget(workflow_id=current, version=number, name=pinned.name, digest=workflow_digest(pinned))
        )
        if len(found) > MAX_TARGETS:
            raise NodestroError(
                "memory.consent_too_large",
                f"A consent covers at most {MAX_TARGETS} workflows (a workflow with its sub-workflows and teams)",
                limit=MAX_TARGETS,
            )
        for node in pinned.nodes:
            for ref in graph.workflow_refs(node):
                queue.append((ref.workflow_id, ref.version))
    return found


__all__ = ["MAX_TARGETS", "ConsentTarget", "agent_digest", "consent_targets", "workflow_digest"]
