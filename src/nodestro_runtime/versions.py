"""The saved versions of each workflow: every save is kept and can be restored.

Undo/redo in the editor works on unsaved changes; this history works on saves. Each
save of a changed workflow stores a snapshot as the next version (``v1``, ``v2``, ...).
Restoring a version never deletes anything: its snapshot becomes the current workflow
and is saved as a new version that notes where it came from, so every earlier state
stays reachable. A workflow saved before versions existed gets its stored state as
``v1`` on the next save.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from .schemas import Workflow, WorkflowIn, now
from .storage import Store

_COLLECTION = "workflow_versions"


class WorkflowVersion(BaseModel):
    workflow_id: str
    number: int
    created_at: datetime = Field(default_factory=now)
    #: An optional note of the user ("before the new research agent").
    description: str = ""
    #: The version this one restored, if it was created by a restore.
    restored_from: int | None = None
    snapshot: WorkflowIn

    @property
    def label(self) -> str:
        return f"v{self.number}"


class VersionSummary(BaseModel):
    workflow_id: str
    number: int
    label: str
    created_at: datetime
    description: str
    restored_from: int | None
    name: str
    agents: int
    nodes: int
    edges: int
    current: bool = False


def _key(workflow_id: str, number: int) -> str:
    return f"{workflow_id}:{number:06d}"


def _content(workflow: WorkflowIn) -> str:
    return WorkflowIn.model_validate(workflow.model_dump()).model_dump_json()


class WorkflowVersions:
    def __init__(self, store: Store) -> None:
        self.store = store

    def all(self, workflow_id: str) -> list[WorkflowVersion]:
        return self.store.list_prefix(_COLLECTION, f"{workflow_id}:", WorkflowVersion)

    def get(self, workflow_id: str, number: int) -> WorkflowVersion:
        version = self.store.get(_COLLECTION, _key(workflow_id, number), WorkflowVersion)
        if version is None:
            raise KeyError(f"v{number}")
        return version

    def record(
        self,
        workflow: Workflow,
        *,
        description: str = "",
        restored_from: int | None = None,
        previous: Workflow | None = None,
    ) -> WorkflowVersion | None:
        """Store the saved state as the next version; ``None`` if it equals the latest one.

        ``previous`` is the state before this save: it becomes ``v1`` when the workflow has no
        versions yet, so a workflow from before the history keeps its old state. Reading the latest
        version and storing the next one is one transaction: two saves at the same time never get
        the same number, and a crash never leaves half of it.
        """
        with self.store.transaction():
            return self._record(workflow, description=description, restored_from=restored_from, previous=previous)

    def _record(
        self, workflow: Workflow, *, description: str, restored_from: int | None, previous: Workflow | None
    ) -> WorkflowVersion | None:
        versions = self.all(workflow.id)
        snapshot = WorkflowIn.model_validate(workflow.model_dump())
        if not versions and previous is not None and _content(previous) != _content(snapshot):
            first = WorkflowVersion(
                workflow_id=workflow.id,
                number=1,
                created_at=previous.updated_at,
                snapshot=WorkflowIn.model_validate(previous.model_dump()),
            )
            self.store.put(_COLLECTION, _key(workflow.id, 1), first)
            versions = [first]
        latest = versions[-1] if versions else None
        if latest is not None and restored_from is None and _content(latest.snapshot) == _content(snapshot):
            if description and not latest.description:
                latest.description = description.strip()[:200]
                self.store.put(_COLLECTION, _key(workflow.id, latest.number), latest)
            return None
        number = (latest.number if latest else 0) + 1
        version = WorkflowVersion(
            workflow_id=workflow.id,
            number=number,
            description=description.strip()[:200],
            restored_from=restored_from,
            snapshot=snapshot,
        )
        self.store.put(_COLLECTION, _key(workflow.id, number), version)
        return version

    def describe(self, workflow_id: str, number: int, description: str) -> WorkflowVersion:
        version = self.get(workflow_id, number)
        version.description = description.strip()[:200]
        self.store.put(_COLLECTION, _key(workflow_id, number), version)
        return version

    def summaries(self, workflow: Workflow) -> list[VersionSummary]:
        """Newest first; ``current`` marks the version equal to the saved workflow."""
        current = _content(WorkflowIn.model_validate(workflow.model_dump()))
        result = []
        marked = False
        for version in reversed(self.all(workflow.id)):
            snapshot = version.snapshot
            is_current = not marked and _content(snapshot) == current
            marked = marked or is_current
            result.append(
                VersionSummary(
                    workflow_id=workflow.id,
                    number=version.number,
                    label=version.label,
                    created_at=version.created_at,
                    description=version.description,
                    restored_from=version.restored_from,
                    name=snapshot.name,
                    agents=sum(node.type == "agent" for node in snapshot.nodes),
                    nodes=len(snapshot.nodes),
                    edges=len(snapshot.edges),
                    current=is_current,
                )
            )
        return result

    def delete_all(self, workflow_id: str) -> None:
        self.store.delete_prefix(_COLLECTION, f"{workflow_id}:")
