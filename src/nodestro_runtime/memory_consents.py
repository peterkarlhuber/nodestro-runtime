"""Which content an owner's consent covers (RT-089d): the digests of workflows and agent configurations.

On a server a memory space is its owner's alone (RT-089c), but a workflow may be shared and edited by other members.
Before RT-089d a member could build a workflow that names another person's space; when that person ran it, its steps
read their memory and could hand it to any tool of the workflow. Now a step opens a space only when its owner allowed
exactly this content (:class:`~nodestro_runtime.memory_spaces.MemoryConsent`):

- **Digest.** :func:`workflow_digest` is the SHA-256 of a workflow's content as a run executes it (its id, nodes,
  edges, limits, notifications; not its time stamps or origin), with every sub-workflow and team pinned to the
  version it will run (:meth:`~nodestro_runtime.engine.RunManager.pin_subworkflows`), and of what its agents use
  outside the workflow (``means``, :meth:`~nodestro_runtime.engine.RunManager.consent_means`): the version and code of
  each custom tool, the settings of each bound connection, the configuration of each MCP server and the definition of
  each MCP tool. A change of any of these (a new version of a custom tool, a connection that now sends elsewhere, an
  edited agent, edge or sub-workflow version) changes the digest; a consent never applies to changed content.
- **Every run that can see the memory.** A step that opens a space needs a consent for its own run's workflow and
  for every run above it (their steps get the step's output). Once a run used a space (``Run.memory_spaces`` of the
  top-level run), every run that executes afterwards in the same tree (a sub-workflow, a team, a continued paused
  run, a continued failed run) needs a consent for its workflow too. So the content can only reach workflow content
  that its owner allowed.
- **Granting.** :func:`consent_targets` lists a workflow and every workflow it runs (sub-workflows, teams,
  recursively, in the versions it would run now), which the owner allows together, confirming them by one
  :func:`combined_digest`; the HTTP API records them.
- **Agent tests** use the digest of the tested configuration with its means (:func:`agent_digest`).

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

#: What an agent uses outside its own configuration, as JSON data that changes whenever one of those means changes
#: (custom tools, connections, MCP servers); the engine's :meth:`~nodestro_runtime.engine.RunManager.consent_means`.
Means = Callable[[AgentConfig], Any]

#: The most workflows one consent covers (a workflow, its sub-workflows and teams); more is refused, never cut.
MAX_TARGETS = 50
#: A workflow's fields that are not its content (time stamps, where a copy came from).
_NOT_CONTENT = frozenset({"created_at", "updated_at", "origin"})


def _digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def workflow_digest(workflow: Workflow, means: Means) -> str:
    """The SHA-256 of ``workflow``'s content and of its agents' ``means`` (see the module documentation); pass it as
    a run executes it (pinned)."""
    fields = set(WorkflowIn.model_fields) | {"id"}
    data = workflow.model_dump(mode="json", include=fields - _NOT_CONTENT)
    used = {node.id: means(node.agent) for node in workflow.nodes if node.agent is not None}
    return _digest({"workflow": data, "means": used})


def agent_digest(config: AgentConfig, means: Means) -> str:
    """The SHA-256 of an agent configuration and its ``means`` (an agent test's consent)."""
    config = AgentConfig.model_validate(config.model_dump())
    return _digest({"agent": config.model_dump(mode="json"), "means": means(config)})


def with_children(digest: str, children: dict[str, str]) -> str:
    """``digest`` together with the digests of the sub-workflows and teams it runs without a saved version (they run
    their current content, which the workflow's own content does not pin); ``digest`` itself without any."""
    return _digest({"own": digest, "children": children}) if children else digest


class ConsentTarget(BaseModel):
    """One workflow a consent covers, as it would run now."""

    workflow_id: str
    version: int | None = None
    name: str = ""
    digest: str


def combined_digest(targets: list[ConsentTarget]) -> str:
    """One digest over all ``targets`` of a consent (ids, versions, digests): what an owner confirms when granting.
    Any change of any of them, or another set of workflows, gives another value."""
    return _digest([[item.workflow_id, item.version, item.digest] for item in targets])


def consent_targets(
    resolve: Callable[[str, int | None], tuple[Workflow, int | None]],
    pin: Callable[[Workflow], Workflow],
    digest: Callable[[Workflow], str],
    workflow_id: str,
    version: int | None = None,
) -> list[ConsentTarget]:
    """The workflow ``workflow_id`` (``version``; ``None``: the latest) and every workflow it runs, recursively, in
    the versions they would run now, each with its digest. ``resolve``, ``pin`` and ``digest`` are the engine's
    (``resolve_workflow``, ``pin_subworkflows``, ``consent_digest``), so the digests are the ones its runs will have.
    Refused with ``memory.consent_too_large`` above :data:`MAX_TARGETS`; an unknown workflow raises ``KeyError``."""
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
        found.append(ConsentTarget(workflow_id=current, version=number, name=pinned.name, digest=digest(pinned)))
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


__all__ = [
    "MAX_TARGETS",
    "ConsentTarget",
    "Means",
    "agent_digest",
    "combined_digest",
    "consent_targets",
    "with_children",
    "workflow_digest",
]
